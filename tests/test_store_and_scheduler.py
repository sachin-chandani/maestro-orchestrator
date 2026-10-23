
from conftest import dt

from maestro.cli import main
from maestro.models import Status
from maestro.parser import parse
from maestro.scheduler import fire_due
from maestro.store import Store

NOW = dt(2026, 10, 23, 18, 0)


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, item):
        self.sent.append(item.text)


def test_round_trip():
    store = Store()
    saved = store.add(parse("pay rent on Nov 1", now=NOW))
    loaded = store.get(saved.id)
    assert loaded.text == "Pay rent"
    assert loaded.due_at == dt(2026, 11, 1, 9, 0)


def test_list_orders_reminders_first_and_hides_done():
    store = Store()
    note = store.add(parse("remember the wifi password is on the router", now=NOW))
    later = store.add(parse("pay rent on Nov 1", now=NOW))
    sooner = store.add(parse("call mom tomorrow at 5pm", now=NOW))
    assert [i.id for i in store.list()] == [sooner.id, later.id, note.id]

    store.set_status(sooner.id, Status.DONE)
    assert sooner.id not in [i.id for i in store.list()]
    assert sooner.id in [i.id for i in store.list(include_done=True)]


def test_search():
    store = Store()
    store.add(parse("remember the spare key is under the mat", now=NOW))
    store.add(parse("buy milk at 7am", now=NOW))
    assert [i.text for i in store.search("key")] == ["The spare key is under the mat"]


def test_fire_due_fires_once():
    store = Store()
    store.add(parse("check the oven in an hour", now=NOW))
    store.add(parse("pay rent on Nov 1", now=NOW))
    notifier = RecordingNotifier()

    assert fire_due(store, notifier, now=dt(2026, 10, 23, 18, 30)) == []
    fired = fire_due(store, notifier, now=dt(2026, 10, 23, 19, 0))
    assert [i.text for i in fired] == ["Check the oven"]
    assert fire_due(store, notifier, now=dt(2026, 10, 23, 19, 5)) == []
    assert notifier.sent == ["Check the oven"]


def test_cli_add_list_done(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    assert main(["--db", db, "add", "remember", "the", "gate", "code", "is", "4321"]) == 0
    assert main(["--db", db, "list"]) == 0
    assert "The gate code is 4321" in capsys.readouterr().out
    assert main(["--db", db, "done", "1"]) == 0
    assert main(["--db", db, "done", "99"]) == 1
