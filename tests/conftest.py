from types import SimpleNamespace

import pytest

from sqlanalyst.data import build
from sqlanalyst.db import ReadOnlyDB


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    return build(tmp_path_factory.mktemp("data") / "trailhead.db")


@pytest.fixture
def db(db_path):
    d = ReadOnlyDB(db_path)
    yield d
    d.close()


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(name, id="t1", **input):
    return SimpleNamespace(type="tool_use", name=name, id=id, input=input)


def response(*content, stop=None):
    if stop is None:
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
    return SimpleNamespace(content=list(content), stop_reason=stop, usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class FakeClient:
    """Plays back scripted responses and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        # Copy the message list, since the agent appends to it after the call.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)
