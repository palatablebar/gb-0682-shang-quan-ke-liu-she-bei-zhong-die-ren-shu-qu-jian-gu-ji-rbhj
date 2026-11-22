from app.errors import DomainError
from app.modules.m5_calculate.adjudication import candidates, PAIR_KEYS
from app.modules.m6_reconcile.verification import derive_fingerprint

def bounds(model):
    zones=model['zones'];keys=sorted(zones);pairs=model['pairs'];values=[zones[k] for k in keys]
    if len(keys)==1:return values[0],values[0],0,0
    if len(keys)==2:
        common=pairs.get('|'.join(keys))
        return (sum(values)-common,sum(values)-common,common,common) if common is not None else (max(values),sum(values),0,min(values))
    a,b,c=keys;ab=pairs['|'.join([a,b])];ac=pairs['|'.join([a,c])];bc=pairs['|'.join([b,c])]
    low=max(0,ab+ac-zones[a],ab+bc-zones[b],ac+bc-zones[c]);high=min(ab,ac,bc)
    if model.get('triple') is not None:low=max(low,model['triple']);high=min(high,model['triple'])
    if low>high:raise DomainError('CONSTRAINT','三方观测不存在可行人数')
    base=sum(values)-ab-ac-bc
    return base+low,base+high,low,high

def _contradiction(bucket, model, line, rowmap):
    """形成可发布的候选修正区间；原始三条交集证据原样挂在裁决记录上。"""
    fingerprint, cand = derive_fingerprint(model)
    evidence=[]
    for key in PAIR_KEYS:
        src=rowmap.get(key)
        evidence.append({'pair':key,'count':model['pairs'][key],
            'line':src['_line'] if src else None,'id':src['id'] if src else None,
            'raw':src['_raw'] if src else None})
    return {'bucket':bucket,'line':line,'status':'待复核','code':'CANDIDATE',
        'zones':dict(model['zones']),'pairs':dict(model['pairs']),
        'evidence':evidence,'fingerprint':fingerprint,**cand}

def apply(state):
    results=[];bad=set();adjudications=[]
    rowmap_by_bucket={}
    for r in state['rows']:
        if r.get('kind')=='pair':
            rowmap_by_bucket.setdefault(r['bucket'],{})[r['left']+'|'+r['right']]=r
    for item in state['models']:
        try:
            low,high,tlo,thi=bounds(item['model']);results.append({'id':item['bucket'],'line':item['line'],'lower':low,'upper':high,'width':high-low,'triple_lower':tlo,'triple_upper':thi,'code':'EXACT' if low==high else 'INTERVAL'})
        except DomainError as e:
            bad.add(item['bucket'])
            # 整桶隔离保留：来源行、ID、原因码与原值均不丢失。
            for r in state['rows']:
                if r['bucket']==item['bucket']:state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':e.code,'raw':r['_raw']})
            if len(item['model']['zones'])==3 and e.code=='CONSTRAINT':
                try:
                    adjudications.append(_contradiction(item['bucket'],item['model'],item['line'],rowmap_by_bucket.get(item['bucket'],{})))
                except DomainError:
                    # 连撤下一条都不可行：维持纯隔离，不伪造候选。
                    pass
    state['rows']=[r for r in state['rows'] if r['bucket'] not in bad]
    state['adjudications']=adjudications
    state['calculations']=results;state['business']={'total':sum(r['lower'] for r in results),'upper_total':sum(r['upper'] for r in results),'buckets':len(results),'unit':'客次'};return state
