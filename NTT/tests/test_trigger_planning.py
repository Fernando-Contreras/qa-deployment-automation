"""Daily_Envs_Plan + QA_Envs + COM_Envs: QUÉ se triggerea y con qué SD/snapshot.

Aquí sólo se prueba la decisión. El formulario de Jenkins está en test_jenkins_forms.py.
"""

from datetime import datetime, timedelta

import pytest

import COM_Envs as com
import Daily_Envs_Plan as plan
import QA_Envs as qa
import Track_Deployments as td

NOW = datetime(2026, 9, 22, 18, 0)
SDCI = "https://sdci.example.com/job/SD.PROJECT/job"
SNAPCI = "https://snapci.example.com/job/SD.PROJECT/job"
SD = "file://storage/Releases/T2.3.49/SD/sd-release.yaml"
SNAP = "nttcom23_ci_48590/nttcom23_ci_nitro_snapshot_ci_20260831_T5"


def deployment(env="06", status="PASSED", started=None, patch=SD, source="env_list", candidates=None, com_env=False):
    family = "nttcom" if com_env else "nttcw"
    return {
        "url": f"{SDCI}/{family}{env}_qa/job/Deploy-Parallel/",
        "status": status,
        "started": started,
        "patch": patch,
        "patch_source": source,
        "patch_candidates": candidates or [patch],
    }


# -------------------------
# Daily_Envs_Plan
# -------------------------
@pytest.mark.parametrize(
    "status, started, bucket",
    [
        ("IN_PROGRESS", NOW, "running"),
        ("FAILED", None, "unknown"),
        ("PASSED", NOW - timedelta(days=1), "trigger"),
        ("FAILED", NOW - timedelta(hours=plan.STALE_HOURS + 1), "ask"),
        ("PASSED", NOW - timedelta(hours=1), "fresh"),
    ],
)
def test_classify(status, started, bucket):
    assert plan.classify(deployment(status=status, started=started), NOW) == bucket


@pytest.mark.parametrize(
    "overrides, problem",
    [
        ({}, None),
        ({"patch": SNAP}, None),
        ({"patch": None}, "su bloque no tiene ningun SD file ni snapshot"),
        ({"patch": "file://storage/sd-release.yml"}, None),
        ({"patch": "file://storage/sd-release.yaml.txt"}, "el SD file no termina en .yaml (posible typo en el Daily_Envs)"),
        ({"source": "block", "candidates": [SD, SNAP]}, "multiple"),
        ({"source": "env_list", "candidates": [SD, SNAP]}, None),
    ],
)
def test_payload_problem_distrusts_ambiguous_paths(overrides, problem):
    kwargs = {k: v for k, v in overrides.items()}
    result = plan.payload_problem(deployment(**kwargs))

    if problem == "multiple":
        assert result.startswith("su bloque tiene 2 rutas distintas")
    else:
        assert result == problem


def test_split_buckets_blocks_the_dubious_ones():
    ok = deployment(env="06", started=NOW - timedelta(days=1))
    dubious = deployment(env="11", started=NOW - timedelta(days=1), patch=None)
    running = deployment(env="80", status="IN_PROGRESS", started=NOW)

    buckets, blocked = plan.split_buckets([ok, dubious, running], NOW)

    assert buckets["trigger"] == [ok]
    assert buckets["running"] == [running]
    assert blocked == [dubious] and dubious["problem"]


def test_read_in_progress_splits_cw_from_com(daily_envs):
    daily_envs("multi_block.txt")

    cw, com_count = plan.read_in_progress("cw")
    com_envs, cw_count = plan.read_in_progress("com")

    assert all(not td.is_com_url(d["url"]) for d in cw)
    assert all(td.is_com_url(d["url"]) for d in com_envs)
    assert len(cw) == cw_count and len(com_envs) == com_count


# -------------------------
# QA_Envs (nttcw)
# -------------------------
def test_qa_urls_pad_the_env_number():
    assert qa.normalize_envs("6, 74,101") == ["06", "74", "101"]
    assert qa.build_sdci_url("06").endswith("/job/nttcw06_qa/job/Deploy-Parallel/build?delay=0sec")
    assert qa.build_snapshot_url("06").endswith("/job/nttcw06_qa/job/Restore-Eatlon-SnapShot/build?delay=0sec")


def test_qa_flags():
    assert qa.parse_args([]) == (False, False, False)
    assert qa.parse_args(["--m"]) == (True, False, False)
    assert qa.parse_args(["--ask"]) == (False, True, False)
    assert qa.parse_args(["--f"]) == (False, False, True)
    assert qa.parse_args(["--f", "--ask"]) == (False, True, True)


SNAP_NO_TENANT = "nttcw46_qa_54086/nttcw46_qa_nitro_snapshot_ci_20261103"
SNAP_T4 = "nttcw46_qa_54086/nttcw46_qa_nitro_snapshot_ci_20261103_T4"


def test_qa_full_auto_skips_snapshots_without_a_tenant(monkeypatch):
    monkeypatch.setattr(qa, "AUTO_TENANT", "")
    sd = deployment(env="06")
    snapshot = deployment(env="11", patch=SNAP_NO_TENANT)

    targets = qa.targets_from_deployments([sd, snapshot], interactive=False)

    assert targets == [{"env": "06", "payload": SD, "snapshot_mode": False, "tenant": None}]


def test_qa_full_auto_uses_the_configured_tenant_when_the_name_has_none(monkeypatch):
    monkeypatch.setattr(qa, "AUTO_TENANT", "4")

    targets = qa.targets_from_deployments([deployment(env="11", patch=SNAP_NO_TENANT)], interactive=False)

    assert targets == [{"env": "11", "payload": SNAP_NO_TENANT, "snapshot_mode": True, "tenant": "4"}]


def test_qa_full_auto_takes_the_tenant_from_the_snapshot_name(monkeypatch):
    """_T4 al final del nombre = tenant 4, sin preguntar ni QA_ENVS_TENANT.
    El nombre manda aunque el env var diga otra cosa."""
    monkeypatch.setattr(qa, "AUTO_TENANT", "")

    targets = qa.targets_from_deployments([deployment(env="43", patch=SNAP_T4)], interactive=False)
    assert targets == [{"env": "43", "payload": SNAP_T4, "snapshot_mode": True, "tenant": "4"}]

    monkeypatch.setattr(qa, "AUTO_TENANT", "9")
    targets = qa.targets_from_deployments([deployment(env="43", patch=SNAP_T4)], interactive=False)
    assert targets[0]["tenant"] == "4"


def test_qa_force_triggers_everything_in_progress_without_checking_jenkins(daily_envs):
    """--f: ni fecha, ni si ya esta corriendo -- lo que diga el Daily_Envs, tal cual.

    No se le pasa navegador ni contexto a force_targets porque no debe tocar
    Jenkins para nada; si lo hiciera, el bloqueo de red de conftest lo tronaria.
    """
    daily_envs("multi_block.txt")

    targets = qa.force_targets()

    assert sorted(t["env"] for t in targets) == ["101", "115", "120", "32", "43", "44", "67", "94"]
    assert all(not t["snapshot_mode"] for t in targets)
    # Los COM del mismo archivo (COM35..COM42) no se cuelan aqui.
    assert all(not t["env"].startswith("COM") for t in targets)


def test_qa_force_still_blocks_dubious_sd_paths(daily_envs):
    """--f no se salta la validacion del SD: eso no depende de Jenkins."""
    daily_envs(text="""## **T2.3.49 — X**
---
🔄 **IN PROGRESS**:
[**06QA**](https://sdci.example.com/job/SD.PROJECT/job/nttcw06_qa/job/Deploy-Parallel/) (T2.3.49)

---
Environment lists:
- T2.3.49 → 06
file://storage/sd-release.yaml.txt
""")

    assert qa.force_targets() == []


# -------------------------
# COM_Envs (nttcom)
# -------------------------
def test_com_envs_are_not_padded():
    assert com.normalize_envs("COM26, COM2, 3") == ["26", "2", "3"]
    assert com.build_sdci_url("2").endswith("/job/nttcom2_qa/job/Deploy-Parallel/build?delay=0sec")


@pytest.mark.parametrize(
    "snapshot, tenant",
    [
        ("nttcom23_ci_48590/nttcom23_ci_nitro_snapshot_ci_20260831_T5", "5"),
        ("nttcom7_ci_49465/snap_t12 ", "12"),
        ("nttcom7_ci_49465/snapshot", None),
        (None, None),
    ],
)
def test_tenant_comes_from_the_snapshot_name(snapshot, tenant):
    assert com.tenant_from_snapshot(snapshot) == tenant


def test_build_url_candidates_start_with_the_daily_envs_link():
    source = f"{SNAPCI}/nttcom26_qa/job/Restore-Eatlon-SnapShot/45/console"

    urls = com.candidate_build_urls(source, "26", snapshot_mode=True)

    assert urls == [
        f"{SNAPCI}/nttcom26_qa/job/Restore-Eatlon-SnapShot/build?delay=0sec",
        f"{SNAPCI}/nttcom26_qa/job/NTT-Create-Eatlon-Snapshot/build?delay=0sec",
    ]


def test_alternate_snapshot_job_names():
    url = f"{SNAPCI}/nttcom26_qa/job/NTT-Create-Eatlon-Snapshot/"

    assert com.alternate_snapshot_urls(url) == [f"{SNAPCI}/nttcom26_qa/job/Restore-Eatlon-SnapShot/"]
    assert com.alternate_snapshot_urls(f"{SDCI}/nttcom2_qa/job/Deploy-Parallel/") == []
    assert com.link_for_daily_envs(
        f"{SNAPCI}/nttcom26_qa/job/Restore-Eatlon-SnapShot/build?delay=0sec", url
    ) == f"{SNAPCI}/nttcom26_qa/job/Restore-Eatlon-SnapShot/"


def test_com_build_targets_deduce_the_tenant_without_asking():
    targets = com.build_targets([deployment(env="26", patch=SNAP, com_env=True)])

    assert targets[0]["tenant"] == "5"
    assert targets[0]["snapshot_mode"] is True


def test_fix_links_in_daily_envs(daily_envs):
    path = daily_envs("com_snapshots_on_hold.txt")
    old = f"{SNAPCI}/nttcom12_qa/job/NTT-Create-Eatlon-Snapshot/"
    new = f"{SNAPCI}/nttcom12_qa/job/Restore-Eatlon-SnapShot/"

    com.fix_links_in_daily_envs([("12", old, new)])

    text = path.read_text(encoding="utf-8")
    assert old not in text and new in text
