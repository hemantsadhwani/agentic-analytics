"""Schema graph, deterministic linking, the SQL writer's guards and the read-only execution.

Offline: SYNTHETIC sample DBs, ``FakeSQLProvider`` or the scripted ``FakeProvider``.
"""

from __future__ import annotations

import json
import sqlite3

import networkx as nx
import pytest

from agentic_reporting import ask, data
from agentic_reporting.providers import FakeProvider
from agentic_reporting.providers.fake import FakeSQLProvider
from agentic_reporting.schema_graph import build_schema_graph, link, to_json, write_json
from agentic_reporting.sql_writer import SQLAnswer, SQLRefused, execute, write_sql
from eval.schema_linking_eval import results_match

REGION_Q = "Which region had the highest net revenue last month?"
SEGMENT_Q = "Net revenue by customer segment last month"


@pytest.fixture(scope="module")
def sales_graph(sales_db):
    return build_schema_graph(sales_db)


@pytest.fixture(scope="module")
def ops_graph(ops_db):
    return build_schema_graph(ops_db)


def _kind(g, kind):
    return {n for n, d in g.nodes(data=True) if d["kind"] == kind}


def _edge(g, u, v):
    return g.edges[u, v]["kind"] if g.has_edge(u, v) else None


# --- build -------------------------------------------------------------------------------

def test_sales_graph_has_expected_nodes_and_edges(sales_graph):
    g = sales_graph
    assert _kind(g, "table") == {"table:orders", "table:customers", "table:products"}
    assert {"column:orders.revenue", "column:orders.customer_id", "column:customers.segment",
            "column:products.category"} <= _kind(g, "column")
    assert _kind(g, "metric") == {"metric:net_revenue", "metric:orders", "metric:mom_change_pct"}
    # ops metrics are not loaded: their tables do not exist in this database
    assert "metric:incident_count" not in g

    join = "join:orders.customer_id=customers.customer_id"
    assert g.nodes[join]["source"] == "foreign_key"
    assert g.nodes["join:orders.product=products.product"]["source"] == "foreign_key"
    assert _edge(g, "table:orders", "column:orders.revenue") == "has_column"
    assert _edge(g, join, "column:orders.customer_id") == "joins_on"
    assert _edge(g, join, "column:customers.customer_id") == "joins_on"
    assert _edge(g, "metric:net_revenue", "column:orders.revenue") == "derives_from"
    assert _edge(g, "metric:mom_change_pct", "metric:net_revenue") == "derives_from"
    assert _edge(g, "term:turnover", "metric:net_revenue") == "synonym_of"
    assert g.nodes["metric:net_revenue"]["expression"] == "ROUND(SUM(orders.revenue), 2)"
    assert g.nodes["metric:net_revenue"]["owner"]


def test_ops_join_is_inferred_from_shared_id_column(ops_graph):
    # services.team_id has no declared FK; teams.team_id is its primary key
    node = ops_graph.nodes["join:services.team_id=teams.team_id"]
    assert node["source"] == "name"
    assert ops_graph.nodes["join:incidents.service=services.service"]["source"] == "foreign_key"
    assert _kind(ops_graph, "metric") == {"metric:incident_count", "metric:mttr_hours",
                                          "metric:sev1_share"}


def test_build_reads_metadata_only_never_a_data_row(sales_db, monkeypatch):
    real = data.connect_ro
    reads: list[str] = []

    def spying_connect(path):
        conn = real(path)

        def auth(action, arg1, *_):
            if action == sqlite3.SQLITE_READ:
                reads.append(arg1)
                if not (arg1 or "").startswith("sqlite_"):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(auth)
        return conn

    monkeypatch.setattr("agentic_reporting.schema_graph.connect_ro", spying_connect)
    g = build_schema_graph(sales_db)
    assert len(_kind(g, "table")) == 3
    assert reads and all(t.startswith("sqlite_") for t in reads)


def test_graph_serialises_to_json(sales_graph, tmp_path):
    path = write_json(sales_graph, tmp_path / "schema_graph.json")
    doc = json.loads(path.read_text())
    assert {n["id"] for n in doc["nodes"]} == set(sales_graph.nodes)
    assert len(doc["edges"]) == sales_graph.number_of_edges()
    assert to_json(sales_graph) == doc


# --- link --------------------------------------------------------------------------------

def test_join_path_between_orders_and_customers(sales_graph):
    r = link(SEGMENT_Q, sales_graph)
    assert r.tables == ["orders", "customers"]
    assert [(j.left_table, j.left_column, j.right_table, j.right_column) for j in r.join_path] == [
        ("orders", "customer_id", "customers", "customer_id")]
    assert "customers.segment" in r.columns
    assert r.metrics == ["net_revenue"]
    # the same path, straight from the graph: table → key column → join → key column → table
    path = nx.shortest_path(sales_graph.to_undirected(as_view=True), "table:orders",
                            "table:customers")
    assert "join:orders.customer_id=customers.customer_id" in path


def test_two_hop_join_path_is_expanded(ops_graph):
    r = link("Which team had the most incidents last month?", ops_graph)
    assert r.tables == ["incidents", "services", "teams"]      # services only via the path
    assert [str(j).split("  ")[0] for j in r.join_path] == [
        "incidents.service = services.service", "services.team_id = teams.team_id"]
    assert "teams.team_name" in r.columns                      # the label, not just the id


def test_synonym_links_to_the_governed_metric(sales_graph):
    r = link("What was our turnover last month?", sales_graph)
    assert r.metrics[0] == "net_revenue"
    assert "turnover" in r.evidence["metric:net_revenue"]
    assert r.metric_definitions["net_revenue"]["expression"] == "ROUND(SUM(orders.revenue), 2)"
    assert r.tables == ["orders"]


def test_more_specific_metric_wins_and_pulls_in_its_inputs(sales_graph):
    r = link("What was the month-over-month change in sales last month?", sales_graph)
    assert r.metrics == ["mom_change_pct", "net_revenue"]


def test_unrelated_question_links_nothing(sales_graph):
    r = link("What is the weather in Paris?", sales_graph)
    assert r.tables == [] and r.metrics == [] and r.confidence == 0.0


def test_link_is_deterministic(sales_graph):
    assert link(SEGMENT_Q, sales_graph).to_dict() == link(SEGMENT_Q, sales_graph).to_dict()


# --- write_sql ---------------------------------------------------------------------------

def test_prompt_carries_only_the_linked_subset(sales_graph):
    provider = FakeSQLProvider()
    write_sql(REGION_Q, link(REGION_Q, sales_graph), provider)
    prompt = provider.calls[0]["messages"][-1]["content"]
    assert "orders" in prompt and "region" in prompt and "ROUND(SUM(orders.revenue), 2)" in prompt
    for absent in ("customers", "segment", "products", "category", "unit_price"):
        assert absent not in prompt


@pytest.mark.parametrize("bad_sql", [
    "UPDATE orders SET revenue = 0",
    "SELECT 1; DROP TABLE orders",
    "DELETE FROM orders",
])
def test_write_sql_refuses_a_write(sales_graph, bad_sql):
    provider = FakeProvider([SQLAnswer(sql=bad_sql, uses_metrics=[], rationale="x")])
    with pytest.raises(SQLRefused, match="unsafe SQL"):
        write_sql(REGION_Q, link(REGION_Q, sales_graph), provider)


def test_write_sql_refuses_a_metric_the_link_did_not_return(sales_graph):
    provider = FakeProvider([SQLAnswer(sql="SELECT COUNT(*) FROM orders",
                                       uses_metrics=["gross_margin"])])
    with pytest.raises(SQLRefused, match="gross_margin"):
        write_sql(REGION_Q, link(REGION_Q, sales_graph), provider)


def test_nothing_linked_means_no_model_call(sales_graph):
    provider = FakeProvider([])
    with pytest.raises(SQLRefused, match="no table"):
        write_sql("What is the weather in Paris?", link("What is the weather in Paris?",
                                                        sales_graph), provider)
    assert provider.calls == []


# --- execute -----------------------------------------------------------------------------

def test_execution_goes_through_the_read_only_path(sales_db, sales_graph, monkeypatch):
    opened = []
    real = data.connect_ro

    def spy(path):
        conn = real(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(data, "connect_ro", spy)
    lr = link(REGION_Q, sales_graph)
    result = execute(sales_db, write_sql(REGION_Q, lr, FakeSQLProvider()), lr)
    assert result.rows == [["North", 33661.95]]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):     # closed after the query
        opened[0].execute("SELECT 1")
    # and a fresh connection from the same path refuses a write
    conn = real(sales_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE orders SET revenue = 0")
    conn.close()


def test_execution_refuses_a_table_outside_the_link(sales_db, sales_graph):
    lr = link(REGION_Q, sales_graph)                  # links orders only
    sneaky = FakeProvider([SQLAnswer(sql="SELECT customer_name FROM customers LIMIT 1",
                                     uses_metrics=[])])
    with pytest.raises(SQLRefused, match="outside the linked subset"):
        execute(sales_db, write_sql(REGION_Q, lr, sneaky), lr)


def test_row_cap_applies(sales_db, sales_graph):
    lr = link("List orders by region", sales_graph)
    big = FakeProvider([SQLAnswer(sql="SELECT order_id, region FROM orders", uses_metrics=[])])
    result = execute(sales_db, write_sql("List orders by region", lr, big), lr)
    assert len(result.rows) == data.ROW_CAP and result.truncated


# --- CLI and eval helper -----------------------------------------------------------------

def test_ask_cli_prints_subset_sql_and_rows(sales_db, tmp_path, capsys):
    out = tmp_path / "g.json"
    assert ask.main(["--db", str(sales_db), "--graph-out", str(out), REGION_Q]) == 0
    text = capsys.readouterr().out
    assert "tables   orders" in text and "net_revenue = ROUND(SUM(orders.revenue), 2)" in text
    assert "SELECT orders.region" in text and "North" in text and "33661.95" in text
    assert json.loads(out.read_text())["nodes"]


def test_ask_cli_stops_before_the_model_when_not_linked(sales_db, tmp_path, capsys):
    rc = ask.main(["--db", str(sales_db), "--graph-out", str(tmp_path / "g.json"),
                   "What is the weather in Paris?"])
    assert rc == 2 and "NOT LINKED" in capsys.readouterr().out


def test_results_match_tolerance():
    assert results_match([["North", 33661.9]], [["North", 33661.95]])
    assert results_match([["b", 2], ["a", 1]], [["a", 1], ["b", 2]])
    assert not results_match([["North", 34000]], [["North", 33661.95]])      # 1% off
    assert not results_match([], [["North", 1]])
    assert not results_match([["South", 1]], [["North", 1]])
