import json
from unittest.mock import MagicMock

import pytest

from live_agent.chrome_launcher import show_window


def test_existing_cdp_creates_and_activates_visible_window(monkeypatch):
    ws = MagicMock()
    replies = [{"targetId": "new-page"}, {"windowId": 123}, {}, {}]

    def receive(**kwargs):
        request = json.loads(ws.send.call_args.args[0])
        return json.dumps({"id": request["id"], "result": replies.pop(0)})

    ws.recv.side_effect = receive
    connection = MagicMock()
    connection.__enter__.return_value = ws
    monkeypatch.setattr("live_agent.chrome_launcher.connect", lambda *args, **kwargs: connection)
    result = show_window({"webSocketDebuggerUrl": "ws://localhost/devtools"}, "about:blank")
    calls = [json.loads(c.args[0]) for c in ws.send.call_args_list]
    assert calls[0]["method"] == "Target.createTarget"
    assert calls[0]["params"] == {"url": "about:blank", "newWindow": True, "background": False}
    assert calls[2]["params"]["bounds"]["windowState"] == "normal"
    assert calls[3]["method"] == "Target.activateTarget"
    assert result["window_id"] == 123


def test_headless_chrome_is_not_reported_as_visible():
    with pytest.raises(RuntimeError, match="无界面"):
        show_window({"User-Agent": "HeadlessChrome/157"}, "about:blank")
