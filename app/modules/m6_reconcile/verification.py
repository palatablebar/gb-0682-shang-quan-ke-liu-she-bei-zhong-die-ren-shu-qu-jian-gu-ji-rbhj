"""候选修正区间的重新核对与正式日汇并入。

只有重新核对通过的候选才能进入正式日汇；核对失败或复核版本陈旧时
调用方负责保持旧版不变。补正重试复用既有裁决记录，不新增记录。
"""
import hashlib
import json
import base64
from app.errors import DomainError
from app.modules.m5_calculate.adjudication import candidates


def derive_fingerprint(model):
    """按原始区域与两两交集证据重新推导候选并生成指纹。"""
    cand = candidates(model)
    material = json.dumps(
        {'zones': model['zones'], 'pairs': model['pairs'], 'candidate': cand},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(material.encode('utf-8')).hexdigest(), cand


def rederive_from_source(state):
    """从批次原始 CSV 字节重新执行完整管线，按桶返回重新推导出的裁决记录。

    独立于已保存的裁决记录，作为重新核对的可信基准。
    """
    encoded = state.get('source_b64')
    if not encoded:
        raise DomainError('RECHECK_FAILED', '批次缺少原始证据，无法重新核对', 409)
    from app.workflow import run
    params = state.get('manifest', {}).get('parameters', {})
    fresh = run(base64.b64decode(encoded), params, stage=5)
    return {a['bucket']: a for a in fresh.get('adjudications', [])}


def recheck(adjudication, fresh=None):
    """重新核对一条裁决记录。

    fresh 为从原始证据重新推导出的同桶裁决；为空时退回记录内自洽核对。
    只有原始证据推导出的指纹与保存记录一致才算通过。
    """
    model = {'zones': adjudication['zones'], 'pairs': adjudication['pairs'], 'triple': None}
    fingerprint, cand = derive_fingerprint(model)
    self_consistent = (adjudication.get('fingerprint') == fingerprint
                       and adjudication['envelope_lower'] == cand['envelope_lower']
                       and adjudication['envelope_upper'] == cand['envelope_upper'])
    if fresh is None:
        return self_consistent, fingerprint, cand
    expected = fresh.get(adjudication['bucket'])
    ok = (self_consistent and expected is not None
          and expected['fingerprint'] == fingerprint
          and expected['envelope_lower'] == adjudication['envelope_lower']
          and expected['envelope_upper'] == adjudication['envelope_upper'])
    return ok, fingerprint, cand


def apply_promotions(state, promotions):
    """把核对通过的补正并入视图：原始 business 与隔离原样保留。

    promotions: {bucket: adjudication with status=='已确认'}
    正式日汇 = 原可估界桶区间之和 + 已确认候选包络之和；
    未确认候选只在 pending 视图中展示，绝不计入正式日汇。
    """
    promoted = state.get('promotions', {})
    for bucket, adj in promotions.items():
        promoted[bucket] = adj
    state['promotions'] = promoted

    base = state.get('business', {})
    confirmed = [a for a in promoted.values() if a.get('status') == '已确认']
    extra_lower = sum(a['envelope_lower'] for a in confirmed)
    extra_upper = sum(a['envelope_upper'] for a in confirmed)
    state['business_official'] = {
        'total': base.get('total', 0) + extra_lower,
        'upper_total': base.get('upper_total', 0) + extra_upper,
        'buckets': base.get('buckets', 0) + len(confirmed),
        'corrected_buckets': len(confirmed),
        'unit': '客次',
    }
    state['pending_adjudication_count'] = sum(
        1 for a in state.get('adjudications', []) if a['bucket'] not in promoted)
    return state


def build_promotion_record(adjudication, fingerprint):
    """补正确认记录；重试同一桶返回同一记录结构，不产生新裁决。"""
    if adjudication.get('status') not in ('待复核', '已确认'):
        raise DomainError('ADJUDICATION', '裁决记录状态不允许补正确认')
    record = dict(adjudication)
    record['fingerprint'] = fingerprint
    record['status'] = '已确认'
    return record
