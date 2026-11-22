from app.modules.m6_reconcile.compare import balance
from app.modules.m6_reconcile.verification import apply_promotions

def execute(state):
    # 已确认补正（来自存储层）在此并入视图；原始隔离与未确认候选保持原样。
    if state.get('promotions'):
        state=apply_promotions(state,state['promotions'])
    state['reconcile']=balance(state);return state
