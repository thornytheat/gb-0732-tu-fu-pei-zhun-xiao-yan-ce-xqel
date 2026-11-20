import json
import shutil
import sqlite3
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from app.config import RULE_VERSION, RunOptions, Settings
from app.errors import RegisterError
from app.manifest import canonical_json
from app.modules.review.archive import read_archive
from app.modules.review.service import recheck_source, run_review
from app.store import Store


LEGACY = 'hx-1978-r2'


def test_archive_recheck_pairs_basis_with_pending_reasons(client):
    sources = client.get('/api/review/migration/sources').json()
    legacy, = [row for row in sources['sources'] if row['id'] == LEGACY]
    assert legacy['kind'] == 'archive' and legacy['rule_version'] != RULE_VERSION
    assert legacy['needs_recheck'] is True

    document = client.post('/api/review/migration/rechecks',
                           json={'source_kind': 'archive', 'source_id': LEGACY}).json()
    assert document['source_state'] == 'MATCHED'
    assert document['source']['rule_version'] != document['rule_version'] == RULE_VERSION
    sheets = {row['code']: row for row in document['sheets']}
    # Every sheet keeps the row-level grounds for a new-rule recheck and states what is still missing.
    assert [sheet['code'] for sheet in document['sheets']] == ['0421', '0422', '0431', '0432']
    for row in sheets.values():
        kinds = [item['kind'] for item in row['basis']]
        assert kinds[:4] == ['input'] * 4
        assert 'controls' in kinds and 'model' in kinds or row['code'] == '0432'
        assert row['pending'] and all(item['label'] for item in row['pending'])
        assert row['signature_state'] in {'STALE', 'INVALIDATED', 'ABSENT'}
    assert sheets['0421']['verdict'] == 'CHANGED'
    assert sheets['0421']['legacy']['status'] == 'READY'
    assert sheets['0421']['recomputed']['status'] == 'RETURN'
    assert sheets['0431']['verdict'] == 'AGREE' and sheets['0431']['signature_state'] == 'STALE'
    assert 'SIGNATURE_STALE' in {item['code'] for item in sheets['0431']['pending']}
    assert sheets['0432']['signature_state'] == 'ABSENT'
    assert document['summary']['CHANGED'] == 2 and document['summary']['AGREE'] == 2


def test_recheck_never_writes_source_batch_or_signatures(client):
    signed = client.post('/api/review/runs', json={}).json()
    run_id = signed['manifest']['run_id']
    url = f'/api/review/runs/{run_id}/signatures'
    assert client.post(url, json={'sheet': '0431', 'reviewer': '周测绘'}).status_code == 200
    before = client.get(f'/api/review/runs/{run_id}').json()

    document = client.post('/api/review/migration/rechecks',
                           json={'source_kind': 'run', 'source_id': run_id}).json()
    assert document['source']['id'] == run_id and document['source_state'] == 'MATCHED'
    after = client.get(f'/api/review/runs/{run_id}').json()
    assert after == before
    assert after['signatures'] == [{'sheet': '0431', 'reviewer': '周测绘',
                                    'signed_at': after['signatures'][0]['signed_at'], 'version': 1}]
    # A recheck is not a signature and does not appear in the source batch's signature list.
    assert len(after['signatures']) == 1


def test_recheck_is_additive_and_idempotent(client):
    run_id = client.post('/api/review/runs', json={}).json()['manifest']['run_id']
    form = {'source_kind': 'run', 'source_id': run_id}
    first = client.post('/api/review/migration/rechecks', json=form).json()
    second = client.post('/api/review/migration/rechecks', json=form).json()
    assert first.pop('reused') is False
    assert second.pop('reused') is True
    assert first == second
    assert client.get(f"/api/review/rechecks/{first['recheck_id']}").json()['recheck_id'] == first['recheck_id']
    export = client.get(f"/api/review/rechecks/{first['recheck_id']}/export")
    assert export.headers['content-disposition'] == f'attachment; filename="map-recheck-{first["recheck_id"][:12]}.json"'
    assert export.json()['format'] == 'map-register/recheck-1'


def test_rerunning_a_legacy_batch_cannot_add_a_second_formal_version(settings):
    store = Store(settings.database)
    store.initialize()
    legacy = read_archive(settings, LEGACY)
    snapshot = build_formal_snapshot(settings, legacy)
    store.insert(snapshot)
    with pytest.raises(RegisterError) as caught:
        run_review(settings, store, RunOptions())
    assert caught.value.code == 'RULE_MIGRATION_REQUIRED'
    assert caught.value.evidence['source_id'] == legacy['manifest']['run_id']
    with sqlite3.connect(settings.database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM runs').fetchone() == (1,)


def test_recheck_does_not_register_a_new_run(settings):
    store = Store(settings.database)
    store.initialize()
    store.insert(build_formal_snapshot(settings, read_archive(settings, LEGACY)))
    document, _ = recheck_source(store, settings, 'archive', LEGACY)
    assert document['sheets']
    with sqlite3.connect(settings.database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM runs').fetchone() == (1,)
        assert connection.execute('SELECT COUNT(*) FROM rechecks').fetchone() == (1,)


def test_changed_source_is_reported_instead_of_recomputed(settings, tmp_path):
    copied = tmp_path / 'sources'
    shutil.copytree(settings.sample_root, copied)
    altered = Settings(sample_root=copied, database=tmp_path / 'register.sqlite3')
    with (copied / 'field' / 'pixels.csv').open('ab') as stream:
        stream.write(b'\n')
    store = Store(altered.database)
    store.initialize()
    document, _ = recheck_source(store, altered, 'archive', LEGACY)
    assert document['source_state'] == 'CHANGED'
    assert all(row['verdict'] == 'UNVERIFIABLE' for row in document['sheets'])
    assert all('SOURCE_CHANGED' in {item['code'] for item in row['pending']} for row in document['sheets'])
    assert all(row['recomputed'] is None for row in document['sheets'])


def test_missing_source_keeps_legacy_outcome_and_names_the_gap(settings, tmp_path):
    bare = Settings(sample_root=tmp_path / 'empty', database=tmp_path / 'register.sqlite3')
    shutil.copytree(settings.sample_root / 'legacy', tmp_path / 'empty' / 'legacy')
    store = Store(bare.database)
    store.initialize()
    document, _ = recheck_source(store, bare, 'archive', LEGACY)
    assert document['source_state'] == 'UNAVAILABLE'
    sheet = next(row for row in document['sheets'] if row['code'] == '0421')
    assert sheet['legacy']['status'] == 'READY' and sheet['recomputed'] is None
    codes = {item['code'] for item in sheet['pending']}
    assert codes == {'SOURCE_UNAVAILABLE', 'EVIDENCE_NOT_RETAINED', 'SIGNATURE_STALE'}
    detail = next(item for item in sheet['pending'] if item['code'] == 'EVIDENCE_NOT_RETAINED')
    assert 'intake.tables' in detail['detail']['missing']
    # The archived signature survives verbatim in the recheck volume.
    assert sheet['signature'] == {'sheet': '0421', 'reviewer': '周测绘',
                                  'signed_at': '2019-03-05T01:20:00+00:00', 'version': 1}


def test_unknown_archive_is_a_clean_404(client):
    assert client.post('/api/review/migration/rechecks',
                       json={'source_kind': 'archive', 'source_id': 'nope'}).status_code == 404
    assert client.get('/api/review/rechecks/' + '0' * 64).status_code == 404


def build_formal_snapshot(settings, legacy):
    """Simulate a batch that the old rule filed before the rule version changed."""
    from app.pipeline import Survey
    from app.modules.review.decisions import decisions
    from app.modules.review.reconcile import reconcile
    survey = Survey(settings, RunOptions())
    old = deepcopy(legacy['manifest'])
    old['inputs'] = {name: sha256(raw).hexdigest() for name, raw in survey.bundle.items()}
    old['parameters'] = RunOptions().model_dump()
    old['run_id'] = sha256(canonical_json({'inputs': old['inputs'], 'parameters': old['parameters'],
                                           'rule_version': old['rule_version']}).encode()).hexdigest()
    return {'manifest': old, 'catalog': survey.catalog,
            'ledger': reconcile(survey.intake, survey.pairing, survey.refit, survey.seams),
            'reviews': decisions(survey.catalog, survey.refit, survey.seams),
            'intake': survey.intake['tables'], 'coordinates': survey.coordinates,
            'pairing': survey.pairing, 'affine': survey.affine,
            'refit': survey.refit, 'seams': survey.seams}
