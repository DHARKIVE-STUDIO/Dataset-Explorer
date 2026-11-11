import io

import pytest
from fastapi.testclient import TestClient

import config
import loader
from app import app

SAMPLE = (config.PROJECT_ROOT / "sample_data" / "store_sales.csv").read_bytes()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def upload(client, content: bytes, name="data.csv"):
    return client.post("/api/datasets", files={"file": (name, io.BytesIO(content), "text/csv")})


@pytest.fixture()
def dataset(client):
    res = upload(client, SAMPLE, "store_sales.csv")
    assert res.status_code == 201, res.text
    return res.json()


def test_upload_and_list(client, dataset):
    assert dataset["row_count"] == 30
    assert dataset["column_count"] == 8
    ids = [d["id"] for d in client.get("/api/datasets").json()["datasets"]]
    assert dataset["id"] in ids
    assert "file_path" not in dataset


def test_profile_types_and_issues(client, dataset):
    p = client.get(f"/api/datasets/{dataset['id']}/profile").json()
    types = {c["name"]: c["type"] for c in p["columns"]}
    assert types["units"] == "numeric"
    assert types["order_date"] == "datetime"
    assert types["region"] == "categorical"
    codes = {i["code"] for i in p["issues"]}
    assert "some_missing" in codes or "many_missing" in codes
    assert any(i["column"] == "rep" for i in p["issues"])


def test_rows_filter_and_sort(client, dataset):
    url = f"/api/datasets/{dataset['id']}/rows"
    res = client.get(url, params={"filters": '[{"column":"region","op":"eq","value":"north"}]'}).json()
    assert res["total"] == 8
    res = client.get(url, params={"filters": '[{"column":"units","op":"gt","value":"10"}]'}).json()
    assert all(row[4] > 10 for row in res["rows"] if row[4] is not None)
    res = client.get(url, params={"sort_by": "units", "sort_dir": "desc", "limit": 3}).json()
    assert res["rows"][0][4] == 30


def test_rows_errors_are_specific(client, dataset):
    url = f"/api/datasets/{dataset['id']}/rows"
    bad_col = client.get(url, params={"filters": '[{"column":"nope","op":"eq","value":"x"}]'})
    assert bad_col.status_code == 400 and bad_col.json()["error"]["code"] == "unknown_column"
    bad_num = client.get(url, params={"filters": '[{"column":"units","op":"gt","value":"lots"}]'})
    assert bad_num.json()["error"]["code"] == "bad_value"
    bad_json = client.get(url, params={"filters": "not json"})
    assert bad_json.json()["error"]["code"] == "invalid_filters"


def test_upload_rejections(client):
    assert upload(client, b"a,b\n1,2\n", name="notes.txt").status_code == 415
    empty = upload(client, b"   \n")
    assert empty.json()["error"]["code"] == "empty_file"
    header_only = upload(client, b"a,b,c\n")
    assert header_only.json()["error"]["code"] == "no_rows"
    malformed = upload(client, b"a,b\n1,2\n3,4,5,6\n")
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "malformed_csv"


def test_upload_size_limit(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 50)
    res = upload(client, b"a,b\n" + b"1,2\n" * 30)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "file_too_large"


def test_tsv_delimiter_detected(client):
    res = upload(client, b"name\tscore\nann\t3\nbo\t5\n", name="scores.tsv")
    assert res.status_code == 201
    assert res.json()["delimiter"] == "\t"
    assert res.json()["column_count"] == 2


def test_charts(client, dataset):
    url = f"/api/datasets/{dataset['id']}/chart"
    hist = client.get(url, params={"kind": "histogram", "x": "units", "bins": 5}).json()
    assert len(hist["labels"]) == 5 and sum(hist["values"]) == 29  # row 1008 has no units
    bar = client.get(url, params={"kind": "bar", "x": "region"}).json()
    assert sum(bar["series"][0]["values"]) == 30
    agg = client.get(url, params={"kind": "bar", "x": "region", "y": "units", "agg": "sum"}).json()
    assert agg["agg"] == "sum" and len(agg["labels"]) == 4
    line = client.get(url, params={"kind": "line", "x": "order_date", "y": "units", "agg": "sum"}).json()
    assert line["labels"][0].startswith("2026-01-03")
    scatter = client.get(url, params={"kind": "scatter", "x": "units", "y": "unit_price"}).json()
    assert len(scatter["points"]) > 0


def test_chart_wrong_type_is_explained(client, dataset):
    res = client.get(f"/api/datasets/{dataset['id']}/chart",
                     params={"kind": "histogram", "x": "region"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "wrong_column_type"


def test_stats(client, dataset):
    stats = client.get(f"/api/datasets/{dataset['id']}/stats", params={"columns": "units,region"}).json()["stats"]
    assert stats["units"]["count"] > 0 and "median" in stats["units"]
    assert stats["region"]["top"][0]["count"] >= 1


def test_insights_without_key_are_labeled_rules(client, dataset):
    res = client.get(f"/api/datasets/{dataset['id']}/insights").json()
    assert res["generated_by"] == "rules"
    assert res["fallback_reason"]
    assert res["suggested_charts"]


def test_persistence_survives_cache_and_restart(client, dataset):
    loader._cache.clear()  # simulate a restart of the in-memory state
    with TestClient(app) as fresh:
        ids = [d["id"] for d in fresh.get("/api/datasets").json()["datasets"]]
        assert dataset["id"] in ids
        res = fresh.get(f"/api/datasets/{dataset['id']}/rows", params={"limit": 2}).json()
        assert res["total"] == 30


def test_delete_removes_metadata_and_file(client, dataset):
    path = config.UPLOAD_DIR / f"{dataset['id']}.csv"
    assert path.exists()
    assert client.delete(f"/api/datasets/{dataset['id']}").status_code == 200
    assert not path.exists()
    assert client.get(f"/api/datasets/{dataset['id']}").json()["error"]["code"] == "dataset_not_found"


def test_heatmap(client, dataset):
    res = client.get(f"/api/datasets/{dataset['id']}/heatmap").json()
    assert len(res["columns"]) >= 2
    assert len(res["matrix"]) == len(res["columns"])
    i = res["columns"].index("units")
    assert abs(res["matrix"][i][i] - 1.0) < 1e-6
    j = res["columns"].index("unit_price")
    assert res["matrix"][i][j] == res["matrix"][j][i]


def test_multi_series_bar(client, dataset):
    url = f"/api/datasets/{dataset['id']}/chart"
    res = client.get(url, params={"kind": "bar", "x": "region", "y": "units,unit_price", "agg": "sum"}).json()
    assert [s["name"] for s in res["series"]] == ["units", "unit_price"]
    assert len(res["labels"]) == 4
    assert all(len(s["values"]) == 4 for s in res["series"])


def test_explicit_dual_axes(client, dataset):
    url = f"/api/datasets/{dataset['id']}/chart"
    res = client.get(url, params={"kind": "line", "x": "order_date", "y": "units,unit_price",
                                  "agg": "sum", "scale": "dual"}).json()
    assert [s["axis"] for s in res["series"]] == ["left", "right"]


def test_auto_indexed_for_wide_range_line(client, dataset):
    url = f"/api/datasets/{dataset['id']}/chart"
    res = client.get(url, params={"kind": "line", "x": "order_date",
                                  "y": "units,unit_price,order_id", "agg": "mean"}).json()
    assert res["scale"] == "indexed"
    assert all(abs(s["values"][0] - 100.0) < 1e-6 for s in res["series"])


def test_scale_rules_are_enforced(client, dataset):
    url = f"/api/datasets/{dataset['id']}/chart"
    three = client.get(url, params={"kind": "line", "x": "order_date",
                                    "y": "units,unit_price,order_id", "agg": "sum", "scale": "dual"})
    assert three.json()["error"]["code"] == "bad_scale"
    bar_indexed = client.get(url, params={"kind": "bar", "x": "region", "y": "units",
                                          "agg": "sum", "scale": "indexed"})
    assert bar_indexed.json()["error"]["code"] == "bad_scale"


def test_scatter_needs_exactly_one_y(client, dataset):
    url = f"/api/datasets/{dataset['id']}/chart"
    res = client.get(url, params={"kind": "scatter", "x": "units", "y": "unit_price,order_id"})
    assert res.json()["error"]["code"] == "scatter_needs_one_y"


def _ask(client, dataset, question):
    return client.post(f"/api/datasets/{dataset['id']}/assistant", json={"question": question}).json()


def test_assistant_rules_bar_by_category(client, dataset):
    r = _ask(client, dataset, "compare units by region")
    assert r["generated_by"] == "rules"
    assert r["chart"]["kind"] == "bar" and r["chart"]["x"] == "region" and r["chart"]["ys"] == ["units"]


def test_assistant_rules_several_metrics(client, dataset):
    r = _ask(client, dataset, "compare units and unit_price by region")
    assert r["chart"]["ys"] == ["units", "unit_price"]
    assert r["chart"]["agg"] == "sum"


def test_assistant_rules_distribution(client, dataset):
    r = _ask(client, dataset, "how is units distributed")
    assert r["chart"]["kind"] == "histogram" and r["chart"]["x"] == "units"


def test_assistant_rules_trend(client, dataset):
    r = _ask(client, dataset, "units over time")
    assert r["chart"]["kind"] == "line"
    assert r["chart"]["x"] == "order_date" and r["chart"]["ys"] == ["units"]


def test_assistant_rejects_empty_question(client, dataset):
    res = client.post(f"/api/datasets/{dataset['id']}/assistant", json={"question": ""})
    assert res.status_code == 422


def test_assistant_ai_path_keeps_valid_chart(client, dataset, monkeypatch):
    import assistant
    reply = ('{"answer":"Bars compare regions.","chart":{"kind":"bar","x":"region",'
             '"ys":["units"],"agg":"sum"}}')
    monkeypatch.setattr(assistant, "_call_model", lambda prompt: reply)
    r = _ask(client, dataset, "which region sells the most")
    assert r["generated_by"] == "ai" and r["chart"]["x"] == "region"


def test_assistant_ai_path_drops_invented_columns(client, dataset, monkeypatch):
    import assistant
    reply = ('{"answer":"Use a bar chart.","chart":{"kind":"bar","x":"region",'
             '"ys":["units","made_up_column"],"agg":"sum"}}')
    monkeypatch.setattr(assistant, "_call_model", lambda prompt: reply)
    r = _ask(client, dataset, "compare things")
    assert r["generated_by"] == "ai"
    assert r["answer"] and r["chart"] is None


def test_assistant_rules_heatmap(client, dataset):
    r = _ask(client, dataset, "show me a correlation heatmap")
    assert r["chart"]["kind"] == "heatmap"


def test_assistant_ai_heatmap_accepted(client, dataset, monkeypatch):
    import assistant
    reply = '{"answer":"A heatmap shows which numbers move together.","chart":{"kind":"heatmap","x":null,"ys":[],"agg":"count"}}'
    monkeypatch.setattr(assistant, "_call_model", lambda prompt: reply)
    r = _ask(client, dataset, "which numbers move together")
    assert r["generated_by"] == "ai" and r["chart"]["kind"] == "heatmap"


def test_heatmap_subset(client, dataset):
    res = client.get(f"/api/datasets/{dataset['id']}/heatmap", params={"columns": "units,unit_price"}).json()
    assert res["columns"] == ["units", "unit_price"]
    assert len(res["matrix"]) == 2 and len(res["matrix"][0]) == 2


def test_heatmap_subset_rejects_text_columns(client, dataset):
    res = client.get(f"/api/datasets/{dataset['id']}/heatmap", params={"columns": "units,region"})
    assert res.json()["error"]["code"] == "wrong_column_type"


def test_heatmap_subset_needs_two_columns(client, dataset):
    res = client.get(f"/api/datasets/{dataset['id']}/heatmap", params={"columns": "units"})
    assert res.json()["error"]["code"] == "not_enough_numeric_columns"
