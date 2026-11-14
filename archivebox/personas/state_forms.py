"""Portable browser-state editing and value-free Persona summaries."""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django import forms

from archivebox.misc.system import atomic_write
from archivebox.personas.importers import _merge_auth_storage, _merge_netscape_cookies, _parse_netscape_cookies


def validate_cookies(text):
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.removeprefix("#HttpOnly_").split("\t")
        if len(parts) != 7 or not parts[0] or not parts[2].startswith("/") or not parts[5]:
            raise forms.ValidationError(f"Line {number}: use Netscape cookies.txt format with seven tab-separated columns.")
        if parts[1] not in {"TRUE", "FALSE"} or parts[3] not in {"TRUE", "FALSE"} or not parts[4].isdigit():
            raise forms.ValidationError(f"Line {number}: check the cookie flags and expiry timestamp.")


def validate_auth(text):
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        raise forms.ValidationError("Enter valid auth.json or Playwright storage-state JSON.")
    if not isinstance(payload, dict):
        raise forms.ValidationError("Auth JSON must be an object.")
    for key in ("cookies", "origins", "tabs"):
        if key in payload and not isinstance(payload[key], list):
            raise forms.ValidationError(f"{key} must be a list.")
    for cookie in payload.get("cookies", []):
        if not isinstance(cookie, dict) or any(not isinstance(cookie.get(key), str) for key in ("domain", "name", "value")):
            raise forms.ValidationError("Each cookie needs a domain, name, and value.")
    for key in ("localStorage", "sessionStorage"):
        if key in payload and not isinstance(payload[key], dict):
            raise forms.ValidationError(f"{key} must map origins to stored entries.")
        if any(not isinstance(values, dict) for values in payload.get(key, {}).values()):
            raise forms.ValidationError(f"Each {key} origin must contain named entries.")
    for origin in payload.get("origins", []):
        if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
            raise forms.ValidationError("Each storage origin needs an origin URL.")
        for key in ("localStorage", "sessionStorage"):
            if key in origin and (
                not isinstance(origin[key], list)
                or any(
                    not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("value"), str)
                    for entry in origin[key]
                )
            ):
                raise forms.ValidationError(f"Origin {key} must be a list of entries.")
        databases = origin.get("indexedDB", [])
        if not isinstance(databases, list):
            raise forms.ValidationError("IndexedDB must be a list of exported databases.")
        for database in databases:
            if not isinstance(database, dict) or not isinstance(database.get("name"), str):
                raise forms.ValidationError("Each IndexedDB export needs a database name.")
            if "data" in database:
                try:
                    exported = json.loads(database["data"])
                    storage = exported["data"]
                    if not isinstance(storage, dict):
                        raise ValueError
                    for key, identity in (("tables", "name"), ("data", "tableName")):
                        if not isinstance(storage.get(key, []), list) or any(
                            not isinstance(item, dict) or not isinstance(item.get(identity), str) for item in storage.get(key, [])
                        ):
                            raise ValueError
                    if any(not isinstance(table.get("rows", []), list) for table in storage.get("data", [])):
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise forms.ValidationError("IndexedDB data must contain a valid Dexie JSON export.")
            if "stores" in database:
                if not isinstance(database["stores"], list):
                    raise forms.ValidationError("IndexedDB stores must be a list.")
                for store in database["stores"]:
                    if (
                        not isinstance(store, dict)
                        or not isinstance(store.get("name"), str)
                        or not isinstance(store.get("records", []), list)
                    ):
                        raise forms.ValidationError("Each IndexedDB store needs a name and a list of records.")
                    if any(
                        not isinstance(record, dict) or not ({"value", "valueEncoded"} & record.keys())
                        for record in store.get("records", [])
                    ):
                        raise forms.ValidationError("Each IndexedDB record needs a value.")
    if any(not isinstance(tab, dict) or not isinstance(tab.get("url"), str) for tab in payload.get("tabs", [])):
        raise forms.ValidationError("Each tab needs a URL.")
    for tab in payload.get("tabs", []):
        for key in ("localStorage", "sessionStorage"):
            if key in tab and (
                not isinstance(tab[key], list)
                or any(not isinstance(entry, dict) or not isinstance(entry.get("name"), str) for entry in tab[key])
            ):
                raise forms.ValidationError(f"Tab {key} must contain named entries.")
    return payload


class PersonaStateForm(forms.Form):
    artifact = forms.ChoiceField(required=False, choices=(("auto", "Browser state"), ("cookies", "Cookies"), ("auth", "Auth storage")))
    action = forms.ChoiceField(
        choices=(("merge", "Merge"), ("clear", "Clear"), ("remove-domain", "Remove site"), ("inspect-domains", "Inspect browser sites")),
    )
    domain = forms.CharField(required=False)
    payload = forms.CharField(required=False, strip=False)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("action") == "inspect-domains":
            return cleaned
        if cleaned.get("action") == "remove-domain":
            domain = normalized_domain(cleaned.get("domain"))
            if not domain or any(character in domain for character in "/\\?#@ \t\n\r\x00"):
                self.add_error("domain", "Choose a valid site hostname.")
            cleaned["domain"] = domain
        elif not cleaned.get("artifact"):
            self.add_error("artifact", "Choose a browser-state file.")
        if cleaned.get("action") == "merge":
            payload = cleaned.get("payload", "")
            if cleaned.get("artifact") == "auto":
                cleaned["artifact"] = "auth" if payload.lstrip().startswith(("{", "[")) else "cookies"
            if not payload.strip():
                self.add_error("payload", "Paste or upload browser state to add.")
            elif cleaned.get("artifact") == "cookies":
                validate_cookies(payload)
            elif cleaned.get("artifact") == "auth":
                validate_auth(payload)
        return cleaned

    def apply(self, persona):
        from archivebox.config.common import get_config
        from archivebox.personas.importers import run_browser_profile

        if self.cleaned_data["action"] == "inspect-domains":
            self.profile_inventory = run_browser_profile(
                action="inspect-domains",
                user_data_dir=str(persona.path / "chrome_profile"),
                config=get_config(persona=persona).model_dump(mode="json"),
            )["domains"]
            return
        if self.cleaned_data["action"] == "remove-domain":
            result = remove_domain(persona, self.cleaned_data["domain"])
            self.profile_inventory = result.get("domains")
            return
        artifact = self.cleaned_data["artifact"]
        path = persona.path / ("cookies.txt" if artifact == "cookies" else "auth.json")
        if self.cleaned_data["action"] == "clear":
            persona.ensure_dirs()
            atomic_write(path, "" if artifact == "cookies" else '{"cookies": [], "origins": []}\n')
            return
        try:
            existing = path.read_text() if path.exists() else ""
        except UnicodeError:
            raise forms.ValidationError("The existing browser-state file is not valid UTF-8.")
        if artifact == "auth" and not existing.strip():
            existing = "{}"
        (validate_cookies if artifact == "cookies" else validate_auth)(existing)
        with tempfile.TemporaryDirectory(prefix="archivebox-persona-state-") as directory:
            merged = Path(directory) / path.name
            incoming = Path(directory) / ("incoming-" + path.name)
            merged.write_text(existing)
            incoming.write_text(self.cleaned_data["payload"])
            (_merge_netscape_cookies if artifact == "cookies" else _merge_auth_storage)(merged, incoming)
            writes = {path: merged}
            command = {"cookies_file" if artifact == "cookies" else "auth_file": str(merged)}
            auth_path = persona.path / "auth.json"
            if artifact == "cookies" and auth_path.exists():
                auth = validate_auth(auth_path.read_text() or "{}")
                imported = _parse_netscape_cookies(incoming)
                changed = False
                for cookie in auth.get("cookies", []):
                    key = (cookie["domain"], cookie.get("path", "/"), cookie["name"])
                    if key not in imported:
                        continue
                    domain, _, cookie_path, secure, expiry, name, value = imported[key]
                    cookie.update(value=value, secure=secure == "TRUE", httpOnly=domain.startswith("#HttpOnly_"))
                    if auth.get("SOURCE") == "archivebox-browser-extension" or "expirationDate" in cookie:
                        if int(expiry):
                            cookie["expirationDate"] = int(expiry)
                        else:
                            cookie.pop("expirationDate", None)
                        cookie["session"] = not int(expiry)
                    if auth.get("SOURCE") != "archivebox-browser-extension" or "expires" in cookie:
                        cookie["expires"] = int(expiry) or -1
                    changed = True
                if changed:
                    updated_auth = Path(directory) / "auth.json"
                    updated_auth.write_text(json.dumps(auth))
                    writes[auth_path] = updated_auth
                    command["auth_file"] = str(updated_auth)
            persona.ensure_dirs()
            for destination, staged in writes.items():
                atomic_write(destination, staged.read_text())
            try:
                result = run_browser_profile(
                    action="import-state",
                    user_data_dir=str(persona.path / "chrome_profile"),
                    config=get_config(persona=persona).model_dump(mode="json"),
                    **command,
                )
            except (ValueError, OSError):
                self.profile_synced = False
            else:
                self.profile_synced = True
                self.profile_inventory = result.get("domains")


def normalized_domain(value):
    from urllib.parse import urlsplit

    value = str(value or "").strip()
    try:
        host = urlsplit(value).hostname if "://" in value else value
    except ValueError:
        return ""
    return (host or "").lower().lstrip(".").rstrip(".")


def profile_directories(root):
    return [
        directory
        for directory in [root, *root.glob("*")]
        if directory.is_dir() and any((directory / marker).exists() for marker in ("Preferences", "Cookies", "Network/Cookies"))
    ]


def auth_domain_entries(payload):
    for cookie in payload.get("cookies", []):
        yield normalized_domain(cookie["domain"]), 1
    for kind in ("localStorage", "sessionStorage"):
        for origin, entries in payload.get(kind, {}).items():
            yield normalized_domain(origin), len(entries) if isinstance(entries, (dict, list)) else 1
    for entry in [*payload.get("origins", []), *payload.get("tabs", [])]:
        count = sum(
            len(entry.get(kind, []))
            for kind in ("localStorage", "sessionStorage", "indexedDB")
            if isinstance(entry.get(kind, []), (dict, list))
        )
        yield normalized_domain(entry.get("origin") or entry.get("url")), count


def browser_state_summary(persona, profile_inventory=None):
    """Counts and exact hostnames only: credential values never enter the result."""
    domains = {}
    unavailable = []

    def add(domain, count, source):
        domain = normalized_domain(domain)
        if not domain:
            return
        row = domains.setdefault(domain, {"domain": domain, "count": 0, "sources": {}})
        row["count"] += count
        if count:
            row["sources"][source] = row["sources"].get(source, 0) + count

    root = persona.path / "chrome_profile"
    profile = {"path": str(root.absolute()), "size": 0, "version": "—", "updated": None, "exists": bool(profile_directories(root))}
    try:
        import re

        version_file = root / "Last Version"
        if not version_file.exists():
            version_file = root / "LastVersion"
        if version_file.exists():
            version = version_file.read_text().strip()
            if re.fullmatch(r"[0-9.]+", version):
                profile["version"] = version
        for file in root.rglob("*"):
            if file.is_symlink() or not file.is_file():
                continue
            stat = file.stat()
            profile["size"] += stat.st_size
            updated = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            if not profile["updated"] or updated > profile["updated"]:
                profile["updated"] = updated
        for directory in [] if profile_inventory is not None else profile_directories(root):
            for database in (directory / "Cookies", directory / "Network" / "Cookies"):
                if not database.exists():
                    continue
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.1) as connection:
                    for domain, count in connection.execute("SELECT host_key, count(*) FROM cookies GROUP BY host_key"):
                        add(domain, count, "Chrome")
    except (OSError, ValueError, sqlite3.Error):
        unavailable.append("Chrome profile")

    if profile_inventory is not None:
        for entry in profile_inventory:
            add(entry["domain"], entry.get("cookies", 0), "Chrome cookies")
            if entry.get("storage_bytes", 0) > 0:
                add(entry["domain"], len(entry.get("origins", [])) or 1, "Chrome storage")
            add(entry["domain"], entry.get("session_entries", 0), "Chrome session")

    artifacts = []
    for key, filename in (("cookies", "cookies.txt"), ("auth", "auth.json")):
        path = persona.path / filename
        artifact = {"key": key, "filename": filename, "size": 0, "updated": None}
        try:
            if path.exists():
                stat = path.stat()
                artifact.update(size=stat.st_size, updated=datetime.fromtimestamp(stat.st_mtime, timezone.utc))
                if key == "cookies":
                    validate_cookies(path.read_text())
                    for domain, _, _ in _parse_netscape_cookies(path):
                        add(domain, 1, filename)
                else:
                    for domain, count in auth_domain_entries(validate_auth(path.read_text() or "{}")):
                        add(domain, count, filename)
        except (OSError, ValueError, forms.ValidationError):
            unavailable.append(filename)
        artifacts.append(artifact)
    rows = []
    for domain in sorted(domains):
        row = domains[domain]
        row["sources"] = [{"name": name, "count": count} for name, count in row["sources"].items()]
        rows.append(row)
    return {
        "site_count": len(rows),
        "domains": rows,
        "profile": profile,
        "artifacts": artifacts,
        "unavailable": unavailable,
        "inventory_complete": profile_inventory is not None or not profile["exists"],
    }


def remove_domain(persona, domain):
    from archivebox.config.common import get_config
    from archivebox.personas.importers import run_browser_profile

    contents = {}
    origins = set()
    cookie_path = persona.path / "cookies.txt"
    if cookie_path.exists():
        text = cookie_path.read_text()
        validate_cookies(text)
        contents[cookie_path] = (
            "\n".join(
                line
                for line in text.splitlines()
                if not line
                or (line.startswith("#") and not line.startswith("#HttpOnly_"))
                or normalized_domain(line.removeprefix("#HttpOnly_").split("\t")[0]) != domain
            )
            + "\n"
        )
    auth_path = persona.path / "auth.json"
    if auth_path.exists():
        payload = validate_auth(auth_path.read_text() or "{}")
        payload["cookies"] = [cookie for cookie in payload.get("cookies", []) if normalized_domain(cookie["domain"]) != domain]
        for key in ("localStorage", "sessionStorage"):
            if key in payload:
                origins.update(origin for origin in payload[key] if normalized_domain(origin) == domain)
                payload[key] = {origin: entries for origin, entries in payload[key].items() if normalized_domain(origin) != domain}
        for key in ("origins", "tabs"):
            if key in payload:
                origins.update(
                    entry.get("origin") or entry.get("url")
                    for entry in payload[key]
                    if normalized_domain(entry.get("origin") or entry.get("url")) == domain
                )
                payload[key] = [entry for entry in payload[key] if normalized_domain(entry.get("origin") or entry.get("url")) != domain]
        contents[auth_path] = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    profile_root = persona.path / "chrome_profile"
    result = {}
    if profile_directories(profile_root):
        result = run_browser_profile(
            action="remove-domain",
            user_data_dir=str(profile_root),
            domain=domain,
            origins=sorted(origins),
            config=get_config(persona=persona).model_dump(mode="json"),
        )
    for path, text in contents.items():
        atomic_write(path, text)
    return result
