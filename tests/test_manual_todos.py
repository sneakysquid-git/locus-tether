import manual_todos


def test_add_item_returns_and_persists(isolated_data_dir):
    item = manual_todos.add_item("Buy milk", due_date="today", owner=None)
    assert item["description"] == "Buy milk"
    assert item["id"].startswith("manual:")
    assert item["created_date"]  # non-empty, needed for the "today only" filter

    items = manual_todos.list_items()
    assert len(items) == 1
    assert items[0]["id"] == item["id"]


def test_multiple_items_accumulate(isolated_data_dir):
    manual_todos.add_item("First")
    manual_todos.add_item("Second")
    items = manual_todos.list_items()
    assert len(items) == 2
    assert {i["description"] for i in items} == {"First", "Second"}


def test_delete_item_removes_only_that_item(isolated_data_dir):
    a = manual_todos.add_item("Keep me")
    b = manual_todos.add_item("Delete me")

    deleted = manual_todos.delete_item(b["id"])
    assert deleted is True

    items = manual_todos.list_items()
    assert len(items) == 1
    assert items[0]["id"] == a["id"]


def test_delete_nonexistent_item_returns_false(isolated_data_dir):
    manual_todos.add_item("Something")
    deleted = manual_todos.delete_item("manual:does-not-exist")
    assert deleted is False
    assert len(manual_todos.list_items()) == 1


def test_list_items_empty_when_none_added(isolated_data_dir):
    assert manual_todos.list_items() == []
