from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.errors import RegisterError


class LegacyManifest(BaseModel):
    model_config = ConfigDict(extra='ignore')

    run_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    rule_version: str = Field(min_length=1, max_length=40)
    created_at: str
    data_version: str = ''
    inputs: dict[str, str]
    parameters: dict


class LegacyReview(BaseModel):
    model_config = ConfigDict(extra='ignore')

    code: str
    title: str = ''
    status: str
    reason: str | None = None
    fit_rms_m: float | None = None
    edges: list[str] = Field(default_factory=list)
    failed_edges: list[str] = Field(default_factory=list)
    blocked_edges: list[str] = Field(default_factory=list)


class LegacySignature(BaseModel):
    model_config = ConfigDict(extra='ignore')

    sheet: str
    reviewer: str
    signed_at: str
    version: int = 1


class LegacyArchive(BaseModel):
    model_config = ConfigDict(extra='ignore')

    format: str = ''
    manifest: LegacyManifest
    catalog: dict
    reviews: list[LegacyReview]
    signatures: list[LegacySignature] = Field(default_factory=list)
    ledger: dict | None = None


def archive_root(settings):
    return Path(settings.sample_root) / 'legacy'


def list_archives(settings):
    root = archive_root(settings)
    items = []
    for path in sorted(root.glob('*.json')):
        try:
            bundle = read_archive(settings, path.stem)
        except RegisterError as error:
            items.append({'id': path.stem, 'title': path.stem, 'invalid': error.code,
                          'message': str(error)})
            continue
        manifest = bundle['manifest']
        items.append({'id': path.stem, 'title': bundle['catalog'].get('title', path.stem),
                      'rule_version': manifest['rule_version'], 'created_at': manifest['created_at'],
                      'data_version': manifest.get('data_version'),
                      'dataset': manifest['parameters'].get('dataset'),
                      'sheets': [review['code'] for review in bundle['reviews']],
                      'signed': len(bundle['signatures']),
                      'mode': 'archive'})
    return items


def read_archive(settings, archive_id):
    if not archive_id or any(char in archive_id for char in '/\\') or archive_id.startswith('.'):
        raise RegisterError('LEGACY_NOT_FOUND', '未找到该旧批次', 404)
    path = archive_root(settings) / f'{archive_id}.json'
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RegisterError('LEGACY_NOT_FOUND', '未找到该旧批次', 404, archive_id) from error
    try:
        parsed = LegacyArchive.model_validate_json(raw)
    except ValidationError as error:
        raise RegisterError('LEGACY_INVALID', '旧批次档件不完整', 422,
                            ['.'.join(map(str, issue['loc'])) for issue in error.errors()]) from error
    bundle = parsed.model_dump()
    # The archive is evidence only: rebuild the view the rechecker expects without inventing row detail.
    return {'manifest': {**bundle['manifest'], 'run_id': bundle['manifest']['run_id']},
            'catalog': bundle['catalog'], 'reviews': bundle['reviews'],
            'signatures': bundle['signatures'], 'ledger': bundle['ledger']}


