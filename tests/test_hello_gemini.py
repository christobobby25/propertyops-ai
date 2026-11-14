"""The smoke script is tested with a mocked client so CI never calls Google."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from google.auth.exceptions import DefaultCredentialsError

from scripts import hello_gemini


def _mock_client(monkeypatch, *, text=None, error=None):
    client = MagicMock()
    if error is not None:
        client.models.generate_content.side_effect = error
    else:
        client.models.generate_content.return_value = SimpleNamespace(text=text)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(hello_gemini.genai, "Client", factory)
    return factory, client


def test_success(valid_env, monkeypatch, capsys):
    factory, client = _mock_client(monkeypatch, text="Hello!")

    assert hello_gemini.main() == 0

    factory.assert_called_once_with(vertexai=True, project="test-project", location="global")
    assert client.models.generate_content.call_args.kwargs["model"] == "gemini-test"
    out = capsys.readouterr().out
    assert "Hello!" in out
    assert "gemini-test" in out


def test_config_error_exits_2(valid_env, monkeypatch, capsys):
    valid_env.delenv("GOOGLE_CLOUD_PROJECT")
    factory, _ = _mock_client(monkeypatch, text="unused")

    assert hello_gemini.main() == 2
    factory.assert_not_called()
    assert "GOOGLE_CLOUD_PROJECT" in capsys.readouterr().err


def test_missing_credentials_exits_1(valid_env, monkeypatch, capsys):
    _mock_client(monkeypatch, error=DefaultCredentialsError("no creds"))

    assert hello_gemini.main() == 1
    assert "gcloud auth application-default login" in capsys.readouterr().err


def test_api_error_exits_1(valid_env, monkeypatch, capsys):
    _mock_client(monkeypatch, error=RuntimeError("404 NOT_FOUND"))

    assert hello_gemini.main() == 1
    assert "404 NOT_FOUND" in capsys.readouterr().err
