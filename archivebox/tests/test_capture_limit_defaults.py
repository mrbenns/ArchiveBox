import json

import pytest

from archivebox.tests.conftest import cli_env, run_archivebox_cmd, run_python_cwd


@pytest.mark.parametrize("unlimited", [False, True])
def test_cli_capture_limits_inherit_defaults_and_accept_explicit_zero(initialized_archive, unlimited):
    flags = ["--max-urls=0", "--crawl-max-size=0", "--crawl-timeout=0", "--snapshot-max-size=0"] if unlimited else []
    result = run_archivebox_cmd(
        ["add", "--index-only", "--bg", *flags, "https://example.com/default-limits"],
        cwd=initialized_archive,
        env=cli_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    stdout, stderr, code = run_python_cwd(
        """
import django,json
django.setup()
from archivebox.config.common import get_config
from archivebox.crawls.models import Crawl
from archivebox.core.forms import AddLinkForm
crawl=Crawl.objects.get(urls='https://example.com/default-limits')
keys=['CRAWL_MAX_URLS','CRAWL_MAX_SIZE','CRAWL_TIMEOUT','SNAPSHOT_MAX_SIZE']
config=get_config(crawl=crawl)
form=AddLinkForm()
print(json.dumps({'stored':crawl.config,'effective':{key:config[key] for key in keys},'initial':{name:form.fields[name].initial for name in ['max_urls','crawl_max_size','crawl_timeout','snapshot_max_size']}}))
""",
        cwd=initialized_archive,
    )
    assert code == 0, stderr
    observed = json.loads(stdout.splitlines()[-1])
    defaults = {"CRAWL_MAX_URLS": 10_000, "CRAWL_MAX_SIZE": 100_000_000_000, "CRAWL_TIMEOUT": 86_400, "SNAPSHOT_MAX_SIZE": 3_000_000_000}
    assert observed["effective"] == ({key: 0 for key in defaults} if unlimited else defaults)
    assert observed["initial"] == dict(zip(["max_urls", "crawl_max_size", "crawl_timeout", "snapshot_max_size"], defaults.values()))
    for key in defaults:
        if unlimited:
            assert observed["stored"][key] == 0
        else:
            assert observed["stored"][key] == defaults[key]
