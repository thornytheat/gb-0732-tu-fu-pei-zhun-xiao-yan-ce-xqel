from datetime import datetime, timezone
from time import perf_counter

from app.manifest import run_manifest
from app.errors import RegisterError
from app.modules.review.archive import read_archive
from app.modules.review.decisions import decisions
from app.modules.review.migration import build_recheck
from app.modules.review.reconcile import reconcile
from app.pipeline import Survey
from app.reasons import REASONS


def run_review(settings, store, options):
    started = perf_counter()
    survey = Survey(settings, options)
    manifest = run_manifest(survey, 0)
    existing = store.get(manifest['run_id'], required=False)
    if existing is not None:
        return existing, True
    # A batch already filed under another rule version must be rechecked in place, not filed twice.
    prior = [row for row in store.find_prior_rule(manifest['inputs'], manifest['parameters'])
             if row['rule_version'] != manifest['rule_version']]
    if prior:
        raise RegisterError('RULE_MIGRATION_REQUIRED', '同一旧批次已有正式版本，请走规则迁移复核，不得另立正式批次',
                            409, {'source_kind': 'run', 'source_id': prior[0]['run_id'],
                                  'rule_version': prior[0]['rule_version'],
                                  'current_rule_version': manifest['rule_version'],
                                  'endpoint': '/api/review/migration/rechecks'})
    ledger = reconcile(survey.intake, survey.pairing, survey.refit, survey.seams)
    reviews = decisions(survey.catalog, survey.refit, survey.seams)
    manifest['processing_ms'] = round((perf_counter() - started) * 1000, 3)
    return store.insert({'manifest': manifest, 'catalog': survey.catalog, 'ledger': ledger, 'reviews': reviews,
                         'intake': survey.intake['tables'], 'coordinates': survey.coordinates,
                         'pairing': survey.pairing, 'affine': survey.affine,
                         'refit': survey.refit, 'seams': survey.seams})


def evidence(snapshot):
    return {'format': 'map-register/evidence-1', **snapshot, 'reason_dictionary': REASONS}


def _source(store, settings, source_kind, source_id):
    if source_kind == 'run':
        return store.get(source_id)
    if source_kind == 'archive':
        return read_archive(settings, source_id)
    raise ValueError(source_kind)


def recheck_source(store, settings, source_kind, source_id):
    """Build a rule-migration volume; the source batch and any signature stay read-only."""
    legacy = _source(store, settings, source_kind, source_id)
    document = build_recheck(settings, legacy, source_kind, source_id)
    document['created_at'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    return store.insert_recheck(document)


def recheck_evidence(document):
    return {'format': 'map-register/recheck-1', **document, 'reason_dictionary': REASONS}
