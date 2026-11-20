from hashlib import sha256
from time import perf_counter

from pydantic import ValidationError

from app.config import DATASETS, FILES, MAX_INPUT_BYTES, RULE_VERSION, RunOptions
from app.errors import RegisterError
from app.manifest import canonical_json
from app.modules.review.decisions import decisions
from app.modules.review.reconcile import reconcile
from app.pipeline import Survey

VERDICT_LABELS = {'AGREE': '结论一致', 'CHANGED': '结论改变', 'UNVERIFIABLE': '待补原件'}
SIGNATURE_LABELS = {'CURRENT': '现规则签审', 'STALE': '旧规签审·待补签',
                    'INVALIDATED': '旧规签审·失效', 'ABSENT': '未签审'}
PENDING_LABELS = {
    'SIGNATURE_STALE': '原签审属旧规则版本，不能延用；复算只作依据，须按现规则重新签审',
    'UNSIGNED_AT_SOURCE': '旧批次该幅未签审，复算可签也不等于已签',
    'RECOMPUTATION_NOT_READY': '新口径复算未达可签条件',
    'EVIDENCE_NOT_RETAINED': '旧证据只留当时结论，缺逐行依据；本册另按新口径复算补充',
    'SOURCE_UNAVAILABLE': '原件档件缺失，无法按旧摘要取得复算输入',
    'SOURCE_CHANGED': '原件摘要与旧批次不符，不能按旧批次复算',
    'RECOMPUTE_FAILED': '新口径复算中断',
    'SOURCE_CHANGED_DURING_RECHECK': '复核期间原件发生变化',
    'PARAMETERS_UNREADABLE': '旧批次参数无法按现口径读取',
}
BASIS_LABELS = {
    'input': '原件摘要', 'controls': '控制点源行', 'model': '重估模型',
    'seam': '拼缝见证', 'signature': '原签审', 'ledger': '三表对账',
}
DEMO_NOTE = ('本册只按现规则复算旧批次：不写原件、不改原签审、不新增正式批次，也不替代签审。'
             '旧签审仍以源档为准，复算结论不得冒充签审记录。')


def _pending(code, detail=None):
    return {'code': code, 'label': PENDING_LABELS[code], 'detail': detail}


def _basis(kind, **payload):
    return {'kind': kind, 'label': BASIS_LABELS[kind], **payload}


def _current_hashes(root, dataset):
    if dataset not in DATASETS:
        return {name: None for name in FILES}
    hashes = {}
    for name in FILES:
        try:
            raw = (root / dataset / name).read_bytes()
        except OSError:
            return {name: None for name in FILES}
        if len(raw) > MAX_INPUT_BYTES:
            return {name: None for name in FILES}
        hashes[name] = sha256(raw).hexdigest()
    return hashes


def _source_state(dataset, inputs, current):
    if dataset not in DATASETS or any(value is None for value in current.values()):
        return 'UNAVAILABLE'
    return 'MATCHED' if current == inputs else 'CHANGED'


def _options(parameters):
    known = RunOptions.model_fields
    try:
        return RunOptions(**{key: parameters[key] for key in known if key in parameters})
    except (ValidationError, TypeError) as error:
        raise RegisterError('PARAMETERS_UNREADABLE', '旧批次参数无法按现口径读取', 422,
                            [str(issue) for issue in getattr(error, 'errors', lambda: [])()]) from error


def _recompute(settings, legacy):
    options = _options(legacy['manifest']['parameters'])
    survey = Survey(settings, options)
    actual = {name: sha256(raw).hexdigest() for name, raw in survey.bundle.items()}
    if actual != legacy['manifest']['inputs']:
        raise RegisterError('SOURCE_CHANGED_DURING_RECHECK', '复核期间原件发生变化', 409)
    ledger = reconcile(survey.intake, survey.pairing, survey.refit, survey.seams)
    reviews = {row['code']: row for row in decisions(survey.catalog, survey.refit, survey.seams)}
    return {'survey': survey, 'ledger': ledger, 'reviews': reviews}


def _rows(records, sheet):
    return [row for row in records if row.get('sheet') == sheet]


def _source_ids(records, key='source'):
    return [row[key]['id'] for row in records if row.get(key)]


def _retained_gaps(legacy, code):
    gaps = []
    if not legacy.get('intake'):
        gaps.append('intake.tables')
    pairing = legacy.get('pairing') or {}
    if not any(row.get('sheet') == code for row in pairing.get('pairs', [])):
        gaps.append('pairing.pairs')
    refit = legacy.get('refit') or {}
    if not (refit.get('sheets') or {}).get(code):
        gaps.append(f'refit.sheets.{code}')
    if legacy.get('seams', {}).get('edges') is None:
        gaps.append('seams.edges')
    return gaps


def _control_basis(legacy, recomputed, code):
    if recomputed is not None:
        refit, origin = recomputed['survey'].refit, 'recomputed'
    else:
        refit, origin = legacy.get('refit') or {}, 'archived'
    accepted = _rows(refit.get('accepted', []), code)
    outliers = _rows(refit.get('outliers', []), code)
    isolated = _rows(refit.get('quarantine', []), code)
    if not (accepted or outliers or isolated):
        return None
    return _basis('controls', origin=origin, accepted=len(accepted), outlier=len(outliers),
                  isolated=len(isolated), accepted_sources=_source_ids(accepted, 'pixel_source'),
                  outlier_sources=_source_ids(outliers, 'pixel_source'),
                  isolated_sources=_source_ids(isolated))


def _model_basis(legacy, recomputed, code):
    if recomputed is not None:
        sheet, origin = recomputed['survey'].refit['sheets'].get(code, {}), 'recomputed'
    else:
        sheet, origin = (legacy.get('refit') or {}).get('sheets', {}).get(code, {}), 'archived'
    if not sheet.get('model'):
        return None
    return _basis('model', origin=origin, status=sheet.get('status'), count=sheet.get('count'),
                  rms_m=sheet.get('rms_m'), max_m=sheet.get('max_m'),
                  east=sheet['model'].get('east'), north=sheet['model'].get('north'),
                  condition=sheet['model'].get('condition'), determinant=sheet['model'].get('determinant'))


def _seam_basis(legacy, recomputed, code):
    if recomputed is not None:
        edges, origin = recomputed['survey'].seams['edges'], 'recomputed'
    else:
        edges, origin = legacy.get('seams', {}).get('edges', []), 'archived'
    items = []
    for edge in edges:
        if code not in (edge.get('a'), edge.get('b')):
            continue
        items.append(_basis('seam', origin=origin, edge=edge['code'], status=edge.get('status'),
                            reason=edge.get('reason'), count=edge.get('count'), coverage=edge.get('coverage'),
                            max_m=edge.get('max_m'), limit_m=edge.get('limit_m'),
                            stations=[station['source']['id'] for station in edge.get('stations', [])
                                      if station.get('source')]))
    return items


def _input_basis(inputs, current):
    return [_basis('input', file=name, sha256=inputs.get(name), current_sha256=current.get(name),
                   match=current.get(name) is not None and current.get(name) == inputs.get(name))
            for name in FILES]


def _compare(legacy_review, recomputed_review):
    if recomputed_review is None or legacy_review is None:
        return 'UNVERIFIABLE'
    same = (legacy_review.get('status') == recomputed_review.get('status') and
            legacy_review.get('reason') == recomputed_review.get('reason'))
    return 'AGREE' if same else 'CHANGED'


def _signature_state(signature, source_rule, recomputed_review):
    if not signature:
        return 'ABSENT'
    if source_rule == RULE_VERSION:
        return 'CURRENT'
    if recomputed_review is not None and recomputed_review.get('status') != 'READY':
        return 'INVALIDATED'
    return 'STALE'


def _sheet_entry(legacy, recomputed, code, title, source_rule, source_state, current, recompute_error):
    legacy_review = next((row for row in legacy.get('reviews', []) if row.get('code') == code), None)
    signature = next((row for row in legacy.get('signatures', []) if row.get('sheet') == code), None)
    recomputed_review = recomputed['reviews'].get(code) if recomputed is not None else None
    verdict = _compare(legacy_review, recomputed_review)
    signature_state = _signature_state(signature, source_rule, recomputed_review)

    basis = _input_basis(legacy['manifest']['inputs'], current)
    controls = _control_basis(legacy, recomputed, code)
    if controls:
        basis.append(controls)
    model = _model_basis(legacy, recomputed, code)
    if model:
        basis.append(model)
    basis.extend(_seam_basis(legacy, recomputed, code))
    if recomputed is not None:
        basis.append(_basis('ledger', origin='recomputed', balanced=recomputed['ledger'].get('balanced')))
    if signature:
        basis.append(_basis('signature', origin='archived', reviewer=signature.get('reviewer'),
                            signed_at=signature.get('signed_at'), version=signature.get('version'),
                            rule_version=source_rule))

    pending, gaps = [], _retained_gaps(legacy, code)
    if source_state == 'UNAVAILABLE':
        pending.append(_pending('SOURCE_UNAVAILABLE', {'dataset': legacy['manifest']['parameters'].get('dataset')}))
        if gaps:
            pending.append(_pending('EVIDENCE_NOT_RETAINED', {'missing': gaps}))
    elif source_state == 'CHANGED':
        pending.append(_pending('SOURCE_CHANGED', {'inputs': legacy['manifest']['inputs']}))
        if gaps:
            pending.append(_pending('EVIDENCE_NOT_RETAINED', {'missing': gaps}))
    elif gaps:
        # Old evidence kept only the outcome; the recheck restores row-level grounds by recomputation.
        pending.append(_pending('EVIDENCE_NOT_RETAINED', {'missing': gaps, 'restored_by': 'recomputed'}))
    if recompute_error:
        if recompute_error['code'] == 'SOURCE_CHANGED_DURING_RECHECK':
            pending.append(_pending('SOURCE_CHANGED', {'inputs': legacy['manifest']['inputs']}))
        else:
            pending.append(_pending('RECOMPUTE_FAILED', recompute_error))
    if signature and source_rule != RULE_VERSION:
        pending.append(_pending('SIGNATURE_STALE', {'signed_rule_version': source_rule,
                                                    'rule_version': RULE_VERSION}))
    if not signature and ((legacy_review or {}).get('status') == 'READY' or
                          (recomputed_review or {}).get('status') == 'READY'):
        pending.append(_pending('UNSIGNED_AT_SOURCE', {'sheet': code}))
    if recomputed_review is not None and recomputed_review.get('status') != 'READY':
        pending.append(_pending('RECOMPUTATION_NOT_READY', {'status': recomputed_review.get('status'),
                                                            'reason': recomputed_review.get('reason')}))

    return {'code': code, 'title': title,
            'legacy': _review_brief(legacy_review),
            'recomputed': _review_brief(recomputed_review),
            'signature': signature,
            'verdict': verdict, 'verdict_label': VERDICT_LABELS[verdict],
            'signature_state': signature_state, 'signature_label': SIGNATURE_LABELS[signature_state],
            'basis': basis, 'pending': pending}


def _review_brief(review):
    if review is None:
        return None
    return {key: review.get(key) for key in ('status', 'reason', 'fit_rms_m', 'edges',
                                             'failed_edges', 'blocked_edges')}


def fingerprint(legacy, source_kind, source_id, current):
    manifest = legacy['manifest']
    return {'source_kind': source_kind, 'source_id': source_id,
            'source_rule_version': manifest['rule_version'], 'rule_version': RULE_VERSION,
            'inputs': manifest['inputs'], 'parameters': manifest['parameters'],
            'source_state': _source_state(manifest['parameters'].get('dataset'), manifest['inputs'], current),
            'current_inputs': current}


def build_recheck(settings, legacy, source_kind, source_id):
    started = perf_counter()
    manifest = legacy['manifest']
    source_rule, parameters = manifest['rule_version'], manifest['parameters']
    current = _current_hashes(settings.sample_root, parameters.get('dataset'))
    source_state = _source_state(parameters.get('dataset'), manifest['inputs'], current)

    recomputed, recompute_error = None, None
    if source_state == 'MATCHED':
        try:
            recomputed = _recompute(settings, legacy)
        except RegisterError as error:
            recompute_error = {'code': error.code, 'message': str(error), 'evidence': error.evidence}

    catalog = legacy.get('catalog') or {}
    titles = {sheet['code']: sheet.get('title', '') for sheet in catalog.get('sheets', [])}
    codes = [sheet['code'] for sheet in catalog.get('sheets', [])]
    if not codes:
        codes = sorted({row.get('code') for row in legacy.get('reviews', []) if row.get('code')})
    sheets = [_sheet_entry(legacy, recomputed, code, titles.get(code, ''), source_rule,
                           source_state, current, recompute_error) for code in codes]

    keys = fingerprint(legacy, source_kind, source_id, current)
    counts = {name: sum(row['verdict'] == name for row in sheets) for name in VERDICT_LABELS}
    signatures = {name: sum(row['signature_state'] == name for row in sheets) for name in SIGNATURE_LABELS}
    return {
        'recheck_id': sha256(canonical_json(keys).encode()).hexdigest(), **keys,
        'processing_ms': round((perf_counter() - started) * 1000, 3),
        'source': {'kind': source_kind, 'id': source_id, 'created_at': manifest.get('created_at'),
                   'data_version': manifest.get('data_version'), 'rule_version': source_rule,
                   'dataset': parameters.get('dataset'), 'parameters': parameters,
                   'inputs': manifest['inputs'], 'current_inputs': current, 'source_state': source_state},
        'ledger': recomputed['ledger'] if recomputed is not None else legacy.get('ledger'),
        'recompute_error': recompute_error,
        'sheets': sheets,
        'summary': {'sheets': len(sheets), **counts, **signatures},
        'note': DEMO_NOTE,
    }
