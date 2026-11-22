"""三方两两交集矛盾时的候选修正区间推导。

原则：不猜测哪条交集是“正确”的，也不把候选算成假精确值。
对三条两两交集分别做“撤下一条”的枚举，仅依靠其余两条交集推导
可行联合人数区间；三条替代区间取保守包络（下界取小、上界取大），
作为可发布的候选修正区间，包络宽度即仍存疑范围。
原始三条交集观测一律保留在隔离记录与裁决记录中，不删除、不改写。
"""
from app.errors import DomainError

PAIR_KEYS = ('A|B', 'A|C', 'B|C')


def _retract_bounds(zones, pairs, dropped):
    """撤下 dropped 这条两两交集后的替代上下界。

    设保留的两条交集共享区域为 pivot：
      三方交集 T 的可行范围仍为 [max(0, p+q-pivot), min(p,q)]，
      联合人数 U = 区域和 - p - q + T（与被撤下的交集无关）。
    """
    kept = [k for k in PAIR_KEYS if k != dropped]
    if len(kept) != 2:
        raise DomainError('CONSTRAINT', '撤下约束必须是三条两两交集之一')
    pivot = (set(kept[0].split('|')) & set(kept[1].split('|'))).pop()
    p, q = pairs[kept[0]], pairs[kept[1]]
    t_low = max(0, p + q - zones[pivot])
    t_high = min(p, q)
    if t_low > t_high:
        # 保留的两条交集本身仍互相矛盾：该替代方案不可行，不能产出伪区间。
        return None
    base = sum(zones.values()) - p - q
    return {
        'dropped': dropped,
        'kept': kept,
        'lower': base + t_low,
        'upper': base + t_high,
        'triple_lower': t_low,
        'triple_high': t_high,
    }


def candidates(model):
    """对矛盾的三区域模型枚举三条撤下方案并求保守包络。"""
    zones = model['zones']
    pairs = model['pairs']
    alternatives = []
    for dropped in PAIR_KEYS:
        alt = _retract_bounds(zones, pairs, dropped)
        if alt is not None:
            alternatives.append(alt)
    if not alternatives:
        raise DomainError('CONSTRAINT', '任意撤下一条交集仍无可行人数，无法形成候选区间')
    envelope_lower = min(a['lower'] for a in alternatives)
    envelope_upper = max(a['upper'] for a in alternatives)
    return {
        'alternatives': alternatives,
        'envelope_lower': envelope_lower,
        'envelope_upper': envelope_upper,
        'uncertainty_width': envelope_upper - envelope_lower,
    }
