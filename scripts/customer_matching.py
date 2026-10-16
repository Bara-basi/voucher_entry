"""Shared, reviewable customer alias mappings and candidate-ranking preference."""
import json,unicodedata
from functools import lru_cache
from pathlib import Path
MAPPING_PATH=Path(__file__).resolve().parent.parent/'mappings/customer_alias_mappings.json'
def normalize_customer_name(value):
    value=unicodedata.normalize('NFKD',str(value or '')).casefold()
    return ''.join(c for c in value if c.isalnum() and not unicodedata.combining(c))
@lru_cache(maxsize=1)
def load_mapping():
    if not MAPPING_PATH.exists():return {'aliases':[],'ranking_rules':{'trailing_hyphen_bonus':.05,'minimum_name_score_for_bonus':0.}}
    return json.loads(MAPPING_PATH.read_text(encoding='utf-8'))
def confirmed_alias(name,customers):
    matches=[a for a in load_mapping()['aliases'] if normalize_customer_name(a['alias'])==normalize_customer_name(name)]
    if not matches:return None
    codes={a['platform_code'] for a in matches}
    if len(codes)!=1:raise ValueError('Conflicting confirmed customer alias: '+str(name))
    records=[r for r in customers if str(r['code'])==next(iter(codes))]
    if len(records)!=1:raise ValueError('Mapped customer code is absent or ambiguous: '+str(codes))
    return records[0]
def candidate_preference(code,base_score):
    rules=load_mapping()['ranking_rules']
    preferred=str(code).endswith('-')
    bonus=float(rules['trailing_hyphen_bonus']) if preferred and base_score>=float(rules['minimum_name_score_for_bonus']) else 0.
    return preferred,bonus,base_score+bonus

@lru_cache(maxsize=1)
def resolution_history():
    p=MAPPING_PATH.parent/'customer_resolution_history.json'
    return json.loads(p.read_text(encoding='utf-8'))['records'] if p.exists() else []

def prior_customer_resolution(name,customers):
    matches=[r for r in resolution_history() if r['normalized_alias']==normalize_customer_name(name)]
    codes={r['platform_code'] for r in matches}
    if len(codes)!=1:return None
    records=[r for r in customers if r['code'] in codes]
    return records[0] if len(records)==1 else None
