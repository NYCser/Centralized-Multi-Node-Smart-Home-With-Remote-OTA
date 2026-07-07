import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_init_firebase_reuses_existing_app(monkeypatch):
    import workers.firebase_sync as firebase_sync

    class FakeFirebaseAdmin:
        def __init__(self):
            self._apps = {}

        def get_app(self, name="[DEFAULT]"):
            if name not in self._apps:
                raise ValueError("No app")
            return self._apps[name]

        def initialize_app(self, credential=None, options=None, name="[DEFAULT]"):
            self._apps[name] = {"credential": credential, "options": options}

    fake_firebase_admin = FakeFirebaseAdmin()
    monkeypatch.setattr(firebase_sync, "firebase_admin", fake_firebase_admin)
    monkeypatch.setattr(firebase_sync, "credentials", SimpleNamespace(Certificate=lambda path: object()))
    monkeypatch.setattr(firebase_sync, "firestore", SimpleNamespace(client=lambda: object()))
    monkeypatch.setattr(firebase_sync, "rtdb", object())
    monkeypatch.setattr(firebase_sync.os.path, "exists", lambda path: True)

    firebase_sync.init_firebase()
    firebase_sync.init_firebase()

    assert fake_firebase_admin.get_app("[DEFAULT]") is not None


def test_resolve_sqlite_path_falls_back_to_local_storage(monkeypatch):
    import workers.firebase_sync as firebase_sync

    monkeypatch.delenv("SQLITE_PATH", raising=False)
    monkeypatch.setattr(firebase_sync.sqlite3, "connect", lambda *args, **kwargs: (_ for _ in ()).throw(Exception("no /data")))

    path = firebase_sync._resolve_sqlite_path()

    assert path == firebase_sync.FALLBACK_SQLITE_PATH
    assert os.path.isdir(os.path.dirname(path))


def test_gateway_config_reuses_firebase_sync_app(monkeypatch):
    import workers.firebase_sync as firebase_sync
    import workers.gateway_config as gateway_config

    class FakeFirebaseAdmin:
        def __init__(self):
            self._apps = {}

        def get_app(self, name="[DEFAULT]"):
            if name not in self._apps:
                raise ValueError("No app")
            return self._apps[name]

        def initialize_app(self, credential=None, options=None, name="[DEFAULT]"):
            self._apps[name] = {"credential": credential, "options": options}

    fake_firebase_admin = FakeFirebaseAdmin()
    monkeypatch.setattr(firebase_sync, "firebase_admin", fake_firebase_admin)
    monkeypatch.setattr(firebase_sync, "credentials", SimpleNamespace(Certificate=lambda path: object()))
    monkeypatch.setattr(firebase_sync, "firestore", SimpleNamespace(client=lambda: object()))
    monkeypatch.setattr(firebase_sync, "rtdb", object())
    monkeypatch.setattr(firebase_sync.os.path, "exists", lambda path: True)

    monkeypatch.setattr(gateway_config, "firebase_admin", fake_firebase_admin)
    monkeypatch.setattr(gateway_config, "firestore", SimpleNamespace(client=lambda: object()))

    gateway_config._get_firestore_client()
    assert fake_firebase_admin.get_app("[DEFAULT]") is not None
