"""The rig's users: config, adding one, suggestions, choosing one (CLI, API)."""

import json
import logging
import tomllib

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from octacam import users
from octacam.cameras import CameraSystem
from octacam.cli import app
from octacam.config import RecordingSettings, find_config_file, parse_config
from octacam.controller import RecordingController
from octacam.web.app import create_app

runner = CliRunner()

RIG = """# The 2P rig (comments must survive adding a user)
[record]
fps = 100

[transfer]
directory = "{nas}/default"

[users.MD]
name = "Matthias Durrieu"
transfer_directory = "{nas}/MD/BallPushing_Imaging"

[users.MD.twophoton]
source = "{share}/MD"
"""


@pytest.fixture
def rig(tmp_path):
    nas, share = tmp_path / "nas", tmp_path / "share"
    for path in (
        nas / "MD" / "BallPushing_Imaging",
        nas / "MA",
        nas / "GE",
        share / "MD",
        share / "MA",
    ):
        path.mkdir(parents=True)
    (nas / "notes").mkdir()  # not initials
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    find_config_file(config_dir).write_text(RIG.format(nas=nas, share=share))
    return config_dir, nas, share


def test_users_are_read_from_the_rig_config(rig, caplog):
    config_dir, nas, share = rig
    path = find_config_file(config_dir)
    path.write_text(path.read_text() + '\n[users.bad]\nname = "x"\n')
    caplog.set_level(logging.WARNING, logger="octacam")
    config = parse_config(path)
    assert list(config.users) == ["MD"]
    md = config.users["MD"]
    assert md.name == "Matthias Durrieu"
    assert md.transfer_directory == f"{nas}/MD/BallPushing_Imaging"
    assert md.plugins == {"twophoton": {"source": f"{share}/MD"}}
    assert any('Ignoring user "bad"' in m for m in caplog.messages)


def test_adding_a_user_appends_and_keeps_the_rest(rig):
    config_dir, nas, share = rig
    before = find_config_file(config_dir).read_text()
    config = users.add_user(
        config_dir,
        "MA",
        name="Someone Else",
        transfer_directory=f"{nas}/MA",
        plugins={"twophoton": {"source": f"{share}/MA"}},
    )
    after = find_config_file(config_dir).read_text()
    assert after.startswith(before)  # comments and order untouched
    assert config.users["MA"].plugins == {"twophoton": {"source": f"{share}/MA"}}
    with pytest.raises(ValueError, match="already exists"):
        users.add_user(config_dir, "MA")
    with pytest.raises(ValueError, match="capital letters"):
        users.add_user(config_dir, "ma")
    tomllib.loads(find_config_file(config_dir).read_text())


def test_a_new_user_s_folders_are_suggested_from_the_others(rig):
    config_dir, nas, share = rig
    config = parse_config(find_config_file(config_dir))
    assert users.suggest_paths(config.users, "MA") == {
        "transfer_directory": str(nas / "MA"),
        "plugins": {"twophoton": {"source": str(share / "MA")}},
    }
    # The NAS's other initials folders are people who could be added.
    assert users.discover_initials(config.users) == ["GE", "MA"]
    assert users.path_status(str(nas / "MA")) == "ok"
    assert users.path_status(str(nas / "ZZ")) == "missing"
    assert users.path_status("~") == "ok"  # expanded, as the transfer does


def test_choosing_a_user_sets_their_destination(rig):
    config_dir, nas, _share = rig
    config = parse_config(find_config_file(config_dir))
    assert users.settings_for_user(config, "MD") == {
        "user": "MD",
        "transfer_directory": f"{nas}/MD/BallPushing_Imaging",
    }
    assert users.settings_for_user(config, "")["transfer_directory"] == f"{nas}/default"
    with pytest.raises(ValueError, match="unknown user 'XY'"):
        users.settings_for_user(config, "XY")


@pytest.fixture
def client(rig, tmp_path):
    config_dir, _nas, _share = rig
    config = parse_config(find_config_file(config_dir))
    settings = RecordingSettings.from_config(config)
    settings = settings.updated(relative_directory="261016_Exp/Fly1/001")
    controller = RecordingController(
        CameraSystem.pending(), settings, config_dir=config_dir
    )
    application = create_app(controller, config)
    with TestClient(application) as test_client:
        test_client.controller = controller
        yield test_client
    controller.close()


def test_the_gui_lists_picks_and_adds_users(client, rig):
    config_dir, nas, share = rig
    listing = client.get("/api/users").json()
    assert [u["initials"] for u in listing["users"]] == ["MD"]
    assert listing["active"] == ""
    assert listing["discovered"] == ["GE", "MA"]

    picked = client.post("/api/users/select", json={"initials": "MD"}).json()
    assert picked["active"] == "MD"
    assert picked["settings"]["transfer_directory"] == f"{nas}/MD/BallPushing_Imaging"
    assert client.post("/api/users/select", json={"initials": "XY"}).status_code == 422
    # The user is never set through the generic settings route.
    assert client.put("/api/settings", json={"user": "MA"}).status_code == 422

    guess = client.get("/api/users/suggest", params={"initials": "ma"}).json()
    assert guess["transfer_directory"] == str(nas / "MA")
    assert guess["transfer_status"] == "ok"
    added = client.post(
        "/api/users",
        json={
            "initials": "ma",
            "name": "Someone Else",
            "transfer_directory": guess["transfer_directory"],
            "plugins": {"twophoton": {"source": str(share / "MA")}},
        },
    ).json()
    assert added["active"] == "MA"
    assert [u["initials"] for u in added["users"]] == ["MA", "MD"]
    assert "[users.MA]" in find_config_file(config_dir).read_text()


def test_each_user_gets_their_folder_name_back(client, rig):
    client.post("/api/users/select", json={"initials": "MD"})
    client.put("/api/settings", json={"relative_directory": "261016_MD/Fly3/002"})
    users.add_user(rig[0], "GE", transfer_directory=str(rig[1] / "GE"))
    client.app.state.app_state.config = parse_config(find_config_file(rig[0]))
    client.post("/api/users/select", json={"initials": "GE"})
    client.put("/api/settings", json={"relative_directory": "261016_GE/Fly1/001"})
    back = client.post("/api/users/select", json={"initials": "MD"}).json()
    assert back["settings"]["relative_directory"] == "261016_MD/Fly3/002"


def test_the_last_user_comes_back_at_the_next_launch(rig, tmp_path):
    config_dir, nas, _share = rig
    config = parse_config(find_config_file(config_dir))
    memory = users.UserMemory(last_user="MD")
    users.save_memory(config_dir, memory)
    controller = RecordingController(
        CameraSystem.pending(),
        RecordingSettings.from_config(config),
        config_dir=config_dir,
    )
    create_app(controller, config)
    assert controller.get_settings().user == "MD"
    assert (
        controller.get_settings().transfer_directory == f"{nas}/MD/BallPushing_Imaging"
    )
    controller.close()


def test_ready_checks_say_what_is_missing(client, rig):
    nas = rig[1]
    client.post("/api/users/select", json={"initials": "MD"})
    checks = client.get("/api/ready").json()["checks"]
    by_label = {c["label"]: c for c in checks}
    assert by_label["Transfer destination"]["status"] == "ok"
    assert by_label["Cameras"]["status"] in ("ok", "info", "warn")
    (nas / "MD" / "BallPushing_Imaging").rmdir()
    checks = client.get("/api/ready", params={"refresh": True}).json()["checks"]
    destination = {c["label"]: c for c in checks}["Transfer destination"]
    assert destination["status"] == "warn"
    assert "not reachable" in destination["detail"]


def test_users_cli_lists_and_adds(rig):
    config_dir, nas, share = rig
    result = runner.invoke(app, ["users", "list", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert "MD  Matthias Durrieu" in result.output
    assert "not set up yet: GE, MA" in result.output
    result = runner.invoke(
        app, ["users", "add", str(config_dir), "MA", "--name", "M A"]
    )
    assert result.exit_code == 0, result.output
    assert f"transfer to {nas / 'MA'}" in result.output  # guessed
    config = parse_config(find_config_file(config_dir))
    assert config.users["MA"].plugins == {"twophoton": {"source": str(share / "MA")}}
    result = runner.invoke(app, ["users", "add", str(config_dir), "MA"])
    assert result.exit_code != 0


def test_the_summary_names_who_recorded(tmp_path):
    from octacam.recording_format import build_recording_summary

    settings = RecordingSettings(save_dir=str(tmp_path), user="MD")
    summary = build_recording_summary(settings, [], start_wall_ns=1, aborted=False)
    assert summary["user"] == "MD"
    assert json.dumps(summary)


def test_saving_the_rig_config_keeps_its_users(rig):
    # The GUI's camera Save rewrites the whole file from its parsed copy.
    from octacam.config_writer import load_raw_config, write_config

    config_dir = rig[0]
    users.add_user(
        config_dir,
        "MA",
        transfer_directory="/nas/MA",
        plugins={"twophoton": {"source": "/share/MA"}},
    )
    write_config(config_dir, load_raw_config(config_dir))
    config = parse_config(find_config_file(config_dir))
    assert sorted(config.users) == ["MA", "MD"]
    assert config.users["MA"].plugins == {"twophoton": {"source": "/share/MA"}}


@pytest.mark.parametrize("explicit", [False, True])
def test_headless_record_says_who_it_records_as(rig, monkeypatch, caplog, explicit):
    # Without --user it records as the last GUI user: a different person at
    # the keyboard must see whose folders the data goes to.
    import typer

    import octacam.cli.record as record_cli

    config_dir, nas, _ = rig
    users.save_memory(config_dir, users.UserMemory(last_user="MD"))

    def no_rig(*args, **kwargs):
        raise typer.Exit(3)

    monkeypatch.setattr(record_cli, "open_rig", no_rig)
    monkeypatch.setattr("octacam.cli._setup_logging", lambda: None)
    caplog.set_level(logging.INFO, logger="octacam")
    argv = ["record", str(config_dir)] + (["--user", "MD"] if explicit else [])
    result = runner.invoke(app, argv)
    assert result.exit_code == 3, result.output
    (said,) = [r for r in caplog.records if "Recording as MD" in r.getMessage()]
    assert said.levelno == (logging.INFO if explicit else logging.WARNING)
    message = said.getMessage()
    assert "Matthias Durrieu" in message
    assert f"transfer to {nas / 'MD' / 'BallPushing_Imaging'}" in message
    assert ("the last user on this machine" in message) is not explicit
