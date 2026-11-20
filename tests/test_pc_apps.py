"""
PC Apps Test
Checks how the PC satellite builds its list of things to open from the Start menu, Epic's
manifests and Steam's, with fakes and files in a temporary folder - Windows itself is never
asked.
"""

import hashlib
import json
import logging

from voice_assistant import pc_apps


def start(*names):
    """A Start menu holding these names, each with a target of its own."""
    return lambda: [{"name": name, "target": f"shell:AppsFolder\\{name}.exe"} for name in names]


def no_steam():
    return []


def names(items):
    return [item["name"] for item in items]


def test_build_drops_uninstall_and_duplicates(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "a.item").write_text(json.dumps({"DisplayName": "discord", "bIsApplication": True,
                                             "AppCategories": ["applications"], "CatalogNamespace": "n",
                                             "CatalogItemId": "i", "AppName": "a"}))
    items = pc_apps.build(start("Discord", "Uninstall Discord", "uninstall thing", "Notepad", "Discord™"),
                          str(epic), no_steam)
    # The Start menu's Discord, as it comes first; Epic's of the same name and the one with a mark are dropped.
    assert names(items) == ["Discord", "Notepad"]
    assert items[0] == {"id": hashlib.sha1(b"appshell:AppsFolder\\Discord.exe").hexdigest()[:12], "name": "Discord",
                        "kind": "app", "target": "shell:AppsFolder\\Discord.exe"}


def test_a_place_which_fails_adds_nothing_and_the_rest_still_count(tmp_path, caplog):
    def fails():
        raise OSError(r"C:\Users\someone\secret")

    with caplog.at_level(logging.WARNING):
        items = pc_apps.build(fails, str(tmp_path / "none"), lambda: [])
    assert items == []
    assert "OSError" in caplog.text and "secret" not in caplog.text


def epic_item(name, application=True, categories=("games",), app="Fortnite"):
    # Written as Epic can: the install path's backslashes left unescaped.
    return ('{"DisplayName": "%s", "bIsApplication": %s, "AppCategories": %s, '
            '"InstallLocation": "D:\\Gaming\\unreal\\%s", "CatalogNamespace": "fn", '
            '"CatalogItemId": "4fe75bbc", "AppName": "%s"}'
            % (name, "true" if application else "false", json.dumps(list(categories)), app, app))


def test_epic_lenient_parse_and_content_dropped(tmp_path, caplog):
    (tmp_path / "1.item").write_text(epic_item("Fortnite"))
    (tmp_path / "2.item").write_text(epic_item("LEGO Fortnite Content"))
    (tmp_path / "3.item").write_text(epic_item("Some Plugin", categories=("plugins",)))
    (tmp_path / "4.item").write_text(epic_item("Not An App", application=False))
    (tmp_path / "5.item").write_text("{not json at all")
    with caplog.at_level(logging.WARNING):
        items = pc_apps.build(start(), str(tmp_path), no_steam)
    assert items == [{"id": items[0]["id"], "name": "Fortnite", "kind": "epic",
                      "target": "com.epicgames.launcher://apps/fn%3A4fe75bbc%3AFortnite?action=launch&silent=true"}]
    assert caplog.text.count("Skipped an Epic manifest") == 1


def test_well_formed_epic_json_reads_as_it_is(tmp_path):
    (tmp_path / "1.item").write_text(json.dumps({"DisplayName": "Rocket League", "bIsApplication": True,
                                                 "AppCategories": ["games", "applications"],
                                                 "InstallLocation": "D:\\Gaming\\RL", "CatalogNamespace": "n",
                                                 "CatalogItemId": "i", "AppName": "Sugar"}))
    assert names(pc_apps.epic(str(tmp_path))) == ["Rocket League"]


def acf(appid, name):
    return f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"universe"\t\t"1"\n\t"name"\t\t"{name}"\n}}\n'


def test_steam_manifests_read(tmp_path):
    steam = tmp_path / "Steam"
    other = tmp_path / "Library"
    for folder in (steam, other):
        (folder / "steamapps").mkdir(parents=True)
    # Steam escapes its backslashes, and lists its own folder as a library too.
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
        % (str(steam).replace("\\", "\\\\"), str(other).replace("\\", "\\\\")))
    (steam / "steamapps" / "appmanifest_228980.acf").write_text(acf(228980, "Steamworks Common Redistributables"))
    (steam / "steamapps" / "appmanifest_1.acf").write_text(acf(1, "Proton 9.0"))
    (steam / "steamapps" / "appmanifest_2.acf").write_text(acf(2, "Steam Linux Runtime 3.0 (sniper)"))
    (steam / "steamapps" / "appmanifest_2357570.acf").write_text(acf(2357570, "Overwatch® 2"))
    (other / "steamapps" / "appmanifest_271590.acf").write_text(acf(271590, 'Grand Theft Auto V \\"Legacy\\"'))
    items = pc_apps.build(start(), str(tmp_path / "no epic"), lambda: [str(steam)])
    assert [(item["name"], item["kind"], item["target"]) for item in items] == [
        ("Overwatch® 2", "steam", "steam://rungameid/2357570"),
        ('Grand Theft Auto V "Legacy"', "steam", "steam://rungameid/271590"),
    ]


def test_steam_without_its_library_list_still_reads_its_own_folder(tmp_path):
    (tmp_path / "steamapps").mkdir()
    (tmp_path / "steamapps" / "appmanifest_252950.acf").write_text(acf(252950, "Rocket League"))
    assert names(pc_apps.steam([str(tmp_path)])) == ["Rocket League"]


def test_start_menu_order_beats_epic_then_steam(tmp_path):
    (tmp_path / "1.item").write_text(epic_item("Fortnite"))
    (tmp_path / "steamapps").mkdir()
    (tmp_path / "steamapps" / "appmanifest_9.acf").write_text(acf(9, "Fortnite"))
    items = pc_apps.build(start(), str(tmp_path), lambda: [str(tmp_path)])
    assert [(item["name"], item["kind"]) for item in items] == [("Fortnite", "epic")]
