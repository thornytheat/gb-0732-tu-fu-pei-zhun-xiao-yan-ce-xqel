import unicodedata
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import RULE_VERSION, RunOptions
from app.manifest import canonical_json
from app.modules.review.archive import list_archives
from app.modules.review.service import evidence, recheck_evidence, recheck_source, run_review

router = APIRouter(prefix='/api/review')
RunId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]
RecheckId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]


class SignatureInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    sheet: Annotated[str, Field(pattern=r'^[A-Z0-9][A-Z0-9-]{0,23}$')]
    reviewer: Annotated[str, Field(min_length=1, max_length=30)]
    expected_version: Annotated[int, Field(ge=0)] = 0

    @field_validator('reviewer', mode='before')
    @classmethod
    def reviewer_name(cls, name):
        if not isinstance(name, str):
            return name
        if any(unicodedata.category(char).startswith('C') for char in name):
            raise ValueError('签审员姓名包含控制字符')
        return name.strip()


class RecheckInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source_kind: Literal['run', 'archive']
    source_id: Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,63}$')]


@router.post('/runs')
def run(request: Request, options: RunOptions):
    snapshot, reused = run_review(request.app.state.settings, request.app.state.store, options)
    return {'reused': reused, **snapshot}


@router.get('/runs')
def list_runs(request: Request):
    return {'runs': request.app.state.store.list_runs()}


@router.get('/runs/{run_id}')
def get_run(request: Request, run_id: RunId):
    return request.app.state.store.get(run_id)


@router.post('/runs/{run_id}/signatures')
def signatures(request: Request, run_id: RunId, form: SignatureInput):
    return request.app.state.store.sign(run_id, form.sheet, form.reviewer, form.expected_version)


@router.get('/runs/{run_id}/export')
def export(request: Request, run_id: RunId):
    snapshot = request.app.state.store.get(run_id)
    return Response(canonical_json(evidence(snapshot)) + '\n', media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="map-register-{run_id[:12]}.json"'})


@router.get('/migration/sources')
def migration_sources(request: Request):
    store = request.app.state.store
    archives = list_archives(request.app.state.settings)
    for archive in archives:
        archive['kind'] = 'archive'
        archive['needs_recheck'] = archive.get('rule_version') != RULE_VERSION
    runs = [{'kind': 'run', 'id': row['run_id'], 'title': row.get('data_version') or row['run_id'][:12],
             **{key: value for key, value in row.items() if key != 'run_id'},
             'needs_recheck': row['rule_version'] != RULE_VERSION}
            for row in store.list_runs()]
    return {'current_rule_version': RULE_VERSION, 'sources': archives + runs}


@router.post('/migration/rechecks')
def migration_recheck(request: Request, form: RecheckInput):
    document, reused = recheck_source(request.app.state.store, request.app.state.settings,
                                      form.source_kind, form.source_id)
    return {'reused': reused, **document}


@router.get('/rechecks/{recheck_id}')
def get_recheck(request: Request, recheck_id: RecheckId):
    return request.app.state.store.get_recheck(recheck_id)


@router.get('/rechecks/{recheck_id}/export')
def export_recheck(request: Request, recheck_id: RecheckId):
    document = request.app.state.store.get_recheck(recheck_id)
    return Response(canonical_json(recheck_evidence(document)) + '\n', media_type='application/json',
                    headers={'Content-Disposition':
                             f'attachment; filename="map-recheck-{recheck_id[:12]}.json"'})
