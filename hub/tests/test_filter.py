import pytest

from bridgehub.core import Filter
from .helpers import config, msg


@pytest.mark.parametrize("message,expected", [
    (msg(text='"lead điểm danh'), ["lead"]),
    (msg(text='"LEAD điểm danh'), ["lead"]),
    (msg(text="lead ơi"), []),
    (msg(text='"worker điểm danh'), ["worker"]),
    (msg(text="<@101> điểm danh", mentions=(101,)), ["lead"]),
    (msg(text="tất cả điểm danh"), ["lead", "worker", "deputy"]),
    (msg(text='"lead báo cáo', author=102), ["lead"]),
    (msg(text='"lead', author=999), []),
    (msg(text="tất cả", author=102), ["lead", "worker", "deputy"]),
    (msg(text='"lead "deputy cùng xem'), ["lead", "deputy"]),
    (msg(text='"LEAD "WORKER "DEPUTY'), ["lead", "worker", "deputy"]),
    (msg(text='"leadcon "workerman "deputycon "garden "galaxy'), []),
    (msg(text='" lead'), []),
    (msg(text='“lead'), []),
    (msg(text='"lead!'), ["lead"]),
    (msg(text='"lead_foo'), []),
    (msg(text='"lead', channel_id=99), []),
    (msg(text='"lead', guild_id=99), []),
    (msg(text='tâ\u0301t ca\u0309'), ["lead", "worker", "deputy"]),
    (msg(text="TẤT CẢ", author=103), ["lead", "worker", "deputy"]),
    (msg(text="mention", mentions=(103, 102)), ["worker", "deputy"]),
])
def test_vectors_and_boundaries(tmp_path, message, expected):
    assert Filter(config(tmp_path)).route(message) == expected


def test_eight_peers_ninth_dropped_owner_calling_lu_resets_everyone(tmp_path):
    f = Filter(config(tmp_path))
    for _ in range(8):
        assert f.route(msg(text='"lead', author=102)) == ["lead"]
    assert f.route(msg(text='"lead "deputy', author=102)) == ["deputy"]
    assert f.counts == {"lead": 8, "worker": 0, "deputy": 1}
    assert f.route(msg(text='"worker')) == ["worker"]
    assert f.counts == dict.fromkeys(("lead", "worker", "deputy"), 0)
    assert f.route(msg(text='"lead', author=102)) == ["lead"]


def test_any_owner_message_resets_even_without_call(tmp_path):
    f = Filter(config(tmp_path))
    for _ in range(8):
        f.route(msg(text="tất cả", author=102))
    assert f.route(msg(text="ordinary room message")) == []
    assert f.route(msg(text="tất cả", author=103)) == ["lead", "worker", "deputy"]


def test_wrong_channel_or_stranger_cannot_reset(tmp_path):
    f = Filter(config(tmp_path))
    for _ in range(8):
        f.route(msg(author=102))
    f.route(msg(text="hello", channel_id=99))
    f.route(msg(text="hello", author=999))
    assert f.route(msg(author=102)) == []
