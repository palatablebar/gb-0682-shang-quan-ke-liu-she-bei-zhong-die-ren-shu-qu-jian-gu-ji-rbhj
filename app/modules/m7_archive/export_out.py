import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    # 裁决记录：被撤下约束、替代上下界、候选包络与原始三条交集证据全部留痕，不随确认而丢失。
    for row in state.get('adjudications',[]):
        writer.writerow(['候选裁决',rid,row.get('line',''),row.get('bucket',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    # 补正确认：仅记录重新核对通过并已进入正式日汇的候选；重试不产生重复行。
    for row in (state.get('promotions') or {}).values():
        writer.writerow(['补正确认',rid,row.get('line',''),row.get('bucket',''),'PROMOTED',json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    return '\ufeff'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
