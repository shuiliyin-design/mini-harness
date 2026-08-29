"""Loopback-only HTTP and server-rendered UI over DigestApplication."""

from dataclasses import asdict, is_dataclass
import argparse
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from .application import ApplicationError
from .bootstrap import bootstrap_application, check_readiness


MAX_REQUEST_BYTES = 64 * 1024
JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
SAFE_FAILURE_MESSAGES = {
    "configuration_error": "服务配置尚未就绪。",
    "search_unavailable": "搜索服务暂时不可用，请稍后重试。",
    "generation_incomplete": "本次摘要未能完整生成。",
    "generation_timeout": "Model request timed out",
    "generation_invalid_response": "Model response was invalid",
    "generation_rate_limited": "Model service is rate limited",
    "generation_unavailable": "Model service is unavailable",
    "generation_configuration_error": "Generation configuration is not ready",
    "generation_refusal": "Model declined this request",
    "generation_empty_output": "Model returned no usable output",
    "output_contract_failed": "Generated content failed the output contract",
    "search_timeout": "Search request timed out",
    "search_rate_limited": "Search service is rate limited",
    "search_invalid_response": "Search response was invalid",
    "search_empty_results": "Search returned no usable results",
    "search_configuration_error": "Search configuration is not ready",
    "legacy_failure": "Legacy run failure; stage is unknown",
    "delivery_unknown": "交付结果暂时无法确认，请勿重复发送。",
    "subscription_disabled": "该订阅已停用。",
    "recovery_required": "运行需要管理员通过 CLI 安全恢复。",
    "run_already_active": "该运行正在处理中。",
    "invalid_request": "请求格式无效。",
    "invalid_subscription": "订阅内容无效。",
    "invalid_feedback": "反馈内容无效。",
    "invalid_conversation_message": "订阅描述无效或包含不应保存的敏感内容。",
    "conversation_not_waiting": "该定义会话当前不接受新的回答。",
    "conversation_not_adjustable": "当前关注范围不能继续调整。",
    "conversation_already_committed": "该关注已经创建，不能再调整这份提案。",
    "idempotency_conflict": "同一请求标识不能用于不同内容。",
    "definition_not_accepted": "该定义尚不能提交为订阅。",
    "subscription_commit_failed": "订阅事务未能安全提交，请重试。",
    "unsupported_tracking_intent": "这个关注目标目前还不能被可靠执行，请调整后重试。",
    "condition_binding_invalid": "当前监测状态需要稍后确认。",
    "condition_persist_failed": "这次价格检查未能安全保存，请稍后重试。",
    "evidence_persist_failed": "这次价格检查未能安全保存，请稍后重试。",
    "delivery_rejected": "无法交付该摘要。",
    "version_conflict": "订阅已被更新，请刷新后重试。",
    "not_found": "未找到请求的内容。",
}


def _projection(value):
    if is_dataclass(value):
        return {key: _projection(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_projection(item) for item in value]
    if isinstance(value, list):
        return [_projection(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _projection(item) for key, item in value.items()}
    return value


def _safe_source_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _nav():
    return """<nav aria-label="主要导航">
      <a href="/">更新</a><a href="/following">关注</a>
      <a href="/create">＋ 新建关注</a>
    </nav>"""


def _render_shell(title, body, csrf_token, script="", poll=False):
    poll_value = "true" if poll else "false"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="app-csrf" content="{escape(csrf_token, quote=True)}"><title>{escape(title)}</title>
<style>
*{{box-sizing:border-box}}body{{font:16px system-ui;margin:auto;max-width:720px;padding:16px 16px 76px;background:#f5f6f8;color:#17202a;line-height:1.5}}header{{display:flex;align-items:center;justify-content:space-between;gap:12px}}h1{{font-size:1.65rem;margin:.2rem 0}}h2{{font-size:1.1rem;margin:24px 0 8px}}h3{{margin:.2rem 0}}a{{color:#1457a6}}.primary,button{{display:inline-block;border:0;border-radius:10px;padding:10px 14px;background:#1457a6;color:white;text-decoration:none;font:inherit}}.secondary{{background:#e8eef7;color:#17324f}}.card{{display:block;background:white;border-radius:14px;padding:15px;margin:10px 0;box-shadow:0 1px 5px #ccd;color:inherit;text-decoration:none}}.card p{{margin:.45rem 0}}.meta,.source{{color:#617080;font-size:.9rem}}.state{{display:inline-block;border-radius:99px;padding:3px 9px;background:#e8eef7;font-size:.86rem}}.alert{{border-left:4px solid #b46b00}}.ready{{border-left:4px solid #237b4b}}textarea{{width:100%;min-height:108px;border:1px solid #aab5c0;border-radius:10px;padding:10px;font:inherit}}#status{{min-height:1.5em;color:#435466}}details{{margin:10px 0}}ul{{padding-left:20px}}nav{{position:fixed;bottom:0;left:0;right:0;display:flex;justify-content:center;gap:28px;padding:12px;background:white;border-top:1px solid #d9dee5;z-index:3}}nav a{{text-decoration:none;font-weight:600}}.definition{{display:grid;grid-template-columns:6em 1fr;gap:5px}}.definition dt{{color:#617080}}.definition dd{{margin:0}}.item{{border-top:1px solid #e5e8ec;padding-top:14px;margin-top:14px}}time{{white-space:nowrap}}
</style></head><body data-poll="{poll_value}">{body}{_nav()}<script>{script}</script></body></html>""".encode("utf-8")


def _summary_card(value, css=""):
    preview = f"<p>{escape(value.preview)}</p>" if value.preview else ""
    price = (
        f"<p><strong>最近价格 ¥{value.latest_price}</strong> · "
        f"提醒条件：低于 ¥{value.threshold}</p>"
        if value.workflow_kind == "CONDITION"
        and value.latest_price is not None else ""
    )
    count = f"{value.item_count} 条内容 · " if value.item_count else ""
    return f"""<a class="card {css}" href="/feeds/{value.feed_id}">
      <h3>{escape(value.topic)}</h3>{price}{preview}
      <p>{escape(value.message)}</p>
      <p class="meta">{count}<time>{escape(value.updated_at[:16].replace('T', ' '))}</time></p>
    </a>"""


def _render_updates_page(application, user_id, csrf_token):
    home = application.get_updates_home(user_id)
    sections = []
    if home.ready_updates:
        sections.append("<section><h2>最新内容</h2>" + "".join(
            _summary_card(value, "ready") for value in home.ready_updates
        ) + "</section>")
    if home.needs_attention:
        sections.append("<section><h2>需要留意</h2>" + "".join(
            _summary_card(value, "alert") for value in home.needs_attention
        ) + "</section>")
    if home.preparing:
        sections.append("<section><h2>正在准备</h2>" + "".join(
            _summary_card(value) for value in home.preparing
        ) + "</section>")
    if home.no_updates:
        sections.append("<section><h2>暂时没有新内容</h2>" + "".join(
            _summary_card(value) for value in home.no_updates
        ) + "</section>")
    if not home.has_feeds:
        sections.append("""<section class="card"><h2>还没有更新</h2>
          <p>告诉我一个想持续关注的主题，我会先和你确认范围。</p>
          <a class="primary" href="/create">创建第一个关注</a></section>""")
    body = """<header><div><h1>更新</h1><p>你关注主题的最新变化</p></div>
      <a class="primary" href="/create">＋ 关注</a></header>""" + "".join(sections)
    script = """if(document.body.dataset.poll==='true'){
      setTimeout(()=>location.reload(),2000);
    }"""
    return _render_shell(
        "更新", body, csrf_token, script, poll=bool(home.preparing),
    )


def _cadence_label(value):
    return {
        "daily": "每天", "1h": "每小时", "6h": "每 6 小时",
        "12h": "每 12 小时", "24h": "每天",
    }.get(value, value)


def _definition_html(value, heading):
    focus = "、".join(value.focus_topics) or "不限定"
    language = "中文" if value.language == "zh-CN" else "英文"
    cadence = _cadence_label(value.cadence)
    delivery = "暂不通知" if value.delivery_preference == "none" else "本机通知"
    intent_rows = []
    for label, item in (
        ("目标", value.goal),
        ("关键条件", "、".join(value.constraints)),
        ("提醒条件", value.trigger),
        ("时间范围", value.time_window),
        ("地点", " → ".join(value.locations)),
    ):
        if item:
            intent_rows.append(
                f"<dt>{escape(label)}</dt><dd>{escape(item)}</dd>"
            )
    return f"""<section><h2>{escape(heading)}</h2><dl class="definition">
      <dt>主题</dt><dd>{escape(value.topic)}</dd>
      {''.join(intent_rows)}
      <dt>重点</dt><dd>{escape(focus)}</dd>
      <dt>语言</dt><dd>{escape(language)}</dd>
      <dt>频率</dt><dd>{escape(cadence)}</dd>
      <dt>篇幅</dt><dd>最多 {value.max_chars} 字 / {value.max_items} 条</dd>
      <dt>通知</dt><dd>{escape(delivery)}</dd></dl></section>"""


def _source_html(source):
    url = _safe_source_url(source.url)
    label = source.title
    if source.domain:
        label += " · " + source.domain
    if source.published_at:
        label += " · " + source.published_at[:16].replace("T", " ")
    if url is None:
        return f'<li class="source">{escape(label)}</li>'
    return (f'<li class="source"><a href="{escape(url, quote=True)}" '
            f'rel="noopener noreferrer" target="_blank">{escape(label)}</a></li>')


def _briefing_html(value, expanded=False):
    items = []
    for index, item in enumerate(value.items, 1):
        sources = "".join(_source_html(source) for source in item.sources)
        reasons = "".join(
            f"<li>{escape(reason)}</li>" for reason in item.why_recommended
        )
        items.append(f"""<article class="item"><h3>{index}. {escape(item.title)}</h3>
          <p>{escape(item.summary)}</p><ul>{sources}</ul>
          <details><summary>为什么推荐</summary><ul>{reasons}</ul></details></article>""")
    historical = _definition_html(value.definition, "本期采用的关注范围")
    opened = " open" if expanded else ""
    return f"""<details class="card"{opened}><summary>
      <strong>{escape(value.period_label)}</strong> · {value.item_count} 条内容
      <span class="meta">{escape(value.created_at[:16].replace('T', ' '))}</span>
      </summary>{''.join(items)}{historical}</details>"""


def _condition_update_html(value, expanded=False):
    opened = " open" if expanded else ""
    notification = (
        f'<p class="state">{escape(value.notification_message)}</p>'
        if value.notification_message else ""
    )
    return f"""<details class="card"{opened}><summary>
      <strong>{escape(value.title)}</strong>
      <span class="meta">{escape(value.created_at[:16].replace('T', ' '))}</span>
      </summary><p>{escape(value.summary)}</p>
      <dl class="definition"><dt>路线</dt><dd>{escape(value.origin)} → {escape(value.destination)}（往返）</dd>
      <dt>出行月份</dt><dd>{value.travel_month} 月</dd>
      <dt>最近价格</dt><dd>¥{value.observed_price}</dd>
      <dt>提醒条件</dt><dd>低于 ¥{value.threshold}</dd>
      <dt>观察时间</dt><dd>{escape(value.observed_at[:16].replace('T', ' '))}</dd></dl>
      {notification}</details>"""


def _event_update_html(value, expanded=False):
    opened = " open" if expanded else ""
    notification = (
        f'<p class="state">{escape(value.notification_message)}</p>'
        if value.notification_message else ""
    )
    return f"""<details class="card"{opened}><summary>
      <strong>{escape(value.title)}</strong>
      <span class="meta">{escape(value.created_at[:16].replace('T', ' '))}</span>
      </summary><p>{escape(value.summary)}</p>
      <dl class="definition"><dt>对象</dt><dd>{escape(value.entity)}</dd>
      <dt>新模型</dt><dd>{escape(value.model_name)}</dd>
      <dt>发布时间</dt><dd>{escape(value.occurred_at[:16].replace('T', ' '))}</dd>
      <dt>官方来源</dt><dd><a href="{escape(value.source_url, quote=True)}">{escape(value.source_title)}</a></dd></dl>
      {notification}</details>"""


def _render_feed_page(application, user_id, csrf_token, feed_id):
    detail = application.get_feed_detail(user_id, feed_id)
    state_label = {
        "active": "正在关注", "paused": "已暂停",
        "completed": "已结束",
        "needs_attention": "状态待确认",
    }[detail.feed_state]
    notice = ""
    if detail.workflow_kind == "BRIEFING" and detail.update_state != "ready":
        css = "alert" if detail.update_state in {"failed", "needs_attention"} else ""
        notice = f'<section class="card {css}"><p>{escape(detail.update_message)}</p></section>'
    monitoring = ""
    if detail.workflow_kind == "CONDITION" and detail.condition_monitoring:
        value = detail.condition_monitoring
        latest = (
            f"<p><strong>最近价格 ¥{value.latest_price}</strong></p>"
            if value.latest_price is not None else ""
        )
        monitoring = f"""<section class="card"><h2>当前监测状态</h2>
          <p>深圳 → 武汉 · {value.travel_year or ''} 年 {value.travel_month} 月往返</p>{latest}
          <p>提醒条件：低于 ¥{value.threshold}</p>
          <p>检查频率：{escape(_cadence_label(
              f'{value.cadence_seconds // 3600}h'
              if value.cadence_seconds else ''))}</p>
          <p>{escape(value.message)}</p>
          {f'<p>下次检查：{escape(value.next_due_at[:16].replace("T", " "))}</p>' if value.next_due_at else ''}
          </section>"""
    if detail.workflow_kind == "EVENT" and detail.event_monitoring:
        value = detail.event_monitoring
        monitoring = f"""<section class="card"><h2>当前关注状态</h2>
          <p>正在关注 OpenAI 新模型</p><p>{escape(value.message)}</p>
          {f'<p>下次检查：{escape(value.next_due_at[:16].replace("T", " "))}</p>' if value.next_due_at else ''}
          </section>"""
    history = "".join(
        (_condition_update_html(value, index == 0)
         if getattr(value, "update_kind", "BRIEFING") == "CONDITION"
         else _event_update_html(value, index == 0)
         if getattr(value, "update_kind", "BRIEFING") == "EVENT"
         else _briefing_html(value, index == 0))
        for index, value in enumerate(detail.history)
    ) or '<p class="card">还没有更新。</p>'
    history_heading = (
        "更新历史" if detail.workflow_kind in {"CONDITION", "EVENT"}
        else "资讯历史"
    )
    body = f"""<header><div><a href="/">‹ 返回更新</a><h1>{escape(detail.topic)}</h1></div>
      <span class="state">{escape(state_label)}</span></header>
      <p>{escape(detail.feed_message)}</p>{monitoring}{notice}
      <section><h2>{history_heading}</h2>{history}</section>
      {_definition_html(detail.current_definition, '当前关注范围')}
      <p><a class="secondary primary" href="/following#{escape(detail.feed_id, quote=True)}">在关注列表查看</a></p>"""
    script = """if(document.body.dataset.poll==='true'){
      setTimeout(()=>location.reload(),2000);
    }"""
    return _render_shell(
        detail.topic, body, csrf_token, script,
        poll=detail.update_state == "preparing",
    )


def _render_following_page(application, user_id, csrf_token):
    rows = []
    for item in application.list_subscriptions(user_id):
        detail = application.get_feed_detail(user_id, item.subscription_id)
        state = {
            "active": "正在关注", "paused": "已暂停",
            "completed": "已结束",
            "needs_attention": "状态待确认",
        }[detail.feed_state]
        action = ""
        if detail.feed_state == "active":
            action = (f'<button data-feed="{item.subscription_id}" '
                      f'data-version="{item.version}" data-action="disable">暂停</button>')
        elif detail.feed_state == "paused":
            action = (f'<button data-feed="{item.subscription_id}" '
                      f'data-version="{item.version}" data-action="enable">恢复</button>')
        rows.append(f"""<article class="card" id="{item.subscription_id}">
          <h2>{escape(item.topic)}</h2><p class="state">{state}</p>
          <p>{escape(item.natural_language_request)}</p>
          <a href="/feeds/{item.subscription_id}">查看内容、范围与历史</a> {action}
        </article>""")
    body = """<header><div><h1>关注</h1><p>管理持续关注的主题</p></div>
      <a class="primary" href="/create">＋ 新建</a></header>""" + (
        "".join(rows) if rows else '<section class="card"><p>还没有关注任何主题。</p><a class="primary" href="/create">创建关注</a></section>'
    )
    script = """document.querySelectorAll('button[data-action]').forEach(button=>{
      button.onclick=async()=>{button.disabled=true;const response=await fetch(`/subscriptions/${button.dataset.feed}/${button.dataset.action}`,{method:'POST',headers:{'Content-Type':'application/json','X-Digest-CSRF':document.querySelector('meta[name=app-csrf]').content},body:JSON.stringify({expected_version:Number(button.dataset.version)})});if(response.ok)location.reload();else button.disabled=false;};
    });"""
    return _render_shell("关注", body, csrf_token, script)


CREATE_SCRIPT = """const token=document.querySelector('meta[name=app-csrf]').content;
const status=document.querySelector('#status');
async function call(path,body,extra={}){
 status.textContent='正在处理…';
 const response=await fetch(path,{method:extra.method||'POST',headers:{'Content-Type':'application/json','X-Digest-CSRF':token,...extra.headers},body:body===undefined?undefined:JSON.stringify(body)});
 const result=await response.json();status.textContent=result.error?.message||'已完成';
 if(!response.ok)return result;
 if(result.subscription_id&&result.message){
   localStorage.removeItem('feed-conversation-id');
   const box=document.querySelector('#conversation');box.replaceChildren();
   const title=document.createElement('h2');title.textContent='✓ 已开始关注';
   const message=document.createElement('p');message.textContent=result.message||'订阅成功，正在准备首篇资讯。';
   const progress=document.createElement('p');progress.textContent=result.workflow_kind==='CONDITION'?'正在检查最近价格，达到条件后会出现在“更新”中。':result.workflow_kind==='EVENT'?'正在关注 OpenAI 新模型，验证后会出现在“更新”中。':'首篇资讯正在准备，完成后会出现在“更新”中。';
   const home=document.createElement('a');home.href='/';home.className='primary';home.textContent='返回更新';
   const detail=document.createElement('a');detail.href=`/feeds/${result.subscription_id}`;detail.textContent='查看关注详情';
   box.append(title,message,progress,home,document.createTextNode(' '),detail);status.textContent=message.textContent;
   pollCommitted(result,progress);return result;
 }
 if(result.conversation_id){localStorage.setItem('feed-conversation-id',result.conversation_id);renderConversation(result);return result;}
 return result;
}
function addField(box,label,value){const row=document.createElement('p'),strong=document.createElement('strong');strong.textContent=label+'：';row.append(strong,document.createTextNode(value));box.append(row);}
function renderConversation(v){
 const box=document.querySelector('#conversation');box.replaceChildren();
 const labels={WAITING_FOR_ANSWER:'正在完善关注范围',DEFINITION_ACCEPTED:'请确认关注范围',REJECTED:'当前不能创建这个关注',INCOMPLETE:'还没能确认关注范围',COLLECTING:'正在理解你的回答'};
 const state=document.createElement('h2');state.textContent=labels[v.status]||'正在完善关注范围';box.append(state);
 if(v.processing){const p=document.createElement('p');p.textContent='正在处理上一次回答，请稍后查看。';box.append(p);setTimeout(()=>fetch(`/conversations/${v.conversation_id}`).then(r=>r.json()).then(next=>{if(next.conversation_id)renderConversation(next)}),500);return;}
 if(v.failure_reason){const p=document.createElement('p');p.textContent='已保存你输入的内容，但这次没能继续。';box.append(p);}
 if(v.status==='WAITING_FOR_ANSWER'){
   const q=document.createElement('p');q.textContent=v.question;box.append(q);
   const form=document.createElement('form'),input=document.createElement('textarea'),button=document.createElement('button');input.required=true;input.maxLength=2000;button.textContent='回答';form.append(input,button);box.append(form);
   form.onsubmit=e=>{e.preventDefault();call(`/conversations/${v.conversation_id}/messages`,{message:input.value},{headers:{'Idempotency-Key':crypto.randomUUID()}})};
 } else if(v.status==='DEFINITION_ACCEPTED'){
   const d=v.definition||{},provenance=d.provenance||{};
   const told=document.createElement('section'),inferred=document.createElement('section'),defaults=document.createElement('section');
   const toldTitle=document.createElement('h3'),inferredTitle=document.createElement('h3'),defaultTitle=document.createElement('h3');
   toldTitle.textContent='你告诉我的';inferredTitle.textContent='系统理解（请确认）';defaultTitle.textContent='系统默认设置';
   told.append(toldTitle);inferred.append(inferredTitle);defaults.append(defaultTitle);box.append(told,inferred,defaults);
   const target=name=>{const source=provenance[name]||'PRODUCT_DEFAULT';return source.startsWith('USER_')?told:source==='SYSTEM_INFERRED'?inferred:defaults};
   const add=(name,label,value,show=true)=>{if(show)addField(target(name),label,value)};
   add('topic','关注对象',d.topic||'',Boolean(d.topic));
   add('goal','你的目标',d.goal||'',Boolean(d.goal));
   add('constraints','关键条件',(d.constraints||[]).join('、'),Boolean((d.constraints||[]).length));
   add('trigger','提醒条件',d.trigger||'',Boolean(d.trigger));
   add('time_window','时间范围',d.resolved_time_window||d.time_window||'',Boolean(d.time_window));
   add('locations','地点',(d.locations||[]).join(' → '),Boolean((d.locations||[]).length));
   add('focus_topics','重点方向',(d.focus_topics||[]).join('、')||'不限定',Boolean((d.focus_topics||[]).length)||!String(provenance.focus_topics||'').startsWith('USER_'));
   add('language','语言',d.language==='zh-CN'?'中文':d.language==='en'?'英文':d.language||'');
   const cadenceLabels={daily:'每天','1h':'每小时','6h':'每 6 小时','12h':'每 12 小时','24h':'每天'};
   add('cadence','更新频率',cadenceLabels[d.cadence]||d.cadence||'');
   add('max_items','每期条数',`最多 ${d.max_items} 条`);
   add('max_chars','长度',`最多 ${d.max_chars} 字`);
   add('delivery_preference','通知',d.delivery_preference==='none'?'在产品内查看':'本机通知');
   if(inferred.children.length===1)inferred.remove();
   const note=document.createElement('p');note.textContent='确认后才会创建持续关注。';box.append(note);
   const confirm=document.createElement('button');confirm.textContent='确认订阅';confirm.onclick=()=>call(`/conversations/${v.conversation_id}/subscription`,{});box.append(confirm);
   const form=document.createElement('form'),input=document.createElement('textarea'),button=document.createElement('button');input.required=true;input.maxLength=2000;input.placeholder='告诉我想怎么调整';button.textContent='继续调整';form.append(input,button);box.append(form);
   form.onsubmit=e=>{e.preventDefault();call(`/conversations/${v.conversation_id}/adjustments`,{message:input.value},{headers:{'Idempotency-Key':crypto.randomUUID()}})};
 } else if(v.status==='REJECTED'||v.status==='INCOMPLETE'){
   const p=document.createElement('p');p.textContent=v.status==='REJECTED'?(v.rejection_reason||'当前不能创建这个关注。'):'这次还没能确认你的关注范围。';box.append(p);
   const retry=document.createElement('button');retry.textContent='重新描述';retry.onclick=()=>{localStorage.removeItem('feed-conversation-id');document.querySelector('#create textarea').focus();box.replaceChildren();};box.append(retry);
 }
}
async function pollCommitted(committed,node){
 if(committed.workflow_kind==='CONDITION'){
   const response=await fetch(`/api/feeds/${committed.subscription_id}`);if(!response.ok)return;
   const value=await response.json(),monitor=value.condition_monitoring||{};node.textContent=monitor.message||'正在检查最近价格。';
   if(monitor.status==='MONITORING')setTimeout(()=>pollCommitted(committed,node),500);return;
 }
 if(committed.workflow_kind==='EVENT'){
   const response=await fetch(`/api/feeds/${committed.subscription_id}`);if(!response.ok)return;
   const value=await response.json(),monitor=value.event_monitoring||{};node.textContent=monitor.message||'正在关注 OpenAI 新模型。';
   if(monitor.status==='MONITORING')setTimeout(()=>pollCommitted(committed,node),500);return;
 }
 const response=await fetch(`/subscriptions/${committed.subscription_id}/briefings/latest`);if(!response.ok)return;
 const value=await response.json();const labels={PENDING:'首篇资讯正在准备。',RUNNING:'首篇资讯正在准备。',READY:'首篇资讯已准备好，可以返回更新阅读。',INCOMPLETE:'首篇资讯暂时没有准备好，你的关注仍然有效。',FAILED:'首篇资讯暂时没有准备好，你的关注仍然有效。',BLOCKED:'首篇资讯状态暂时无法确认，我们不会重复生成。'};node.textContent=labels[value.status]||'首篇资讯正在准备。';
 if(value.status==='PENDING'||value.status==='RUNNING')setTimeout(()=>pollCommitted(committed,node),500);
}
document.querySelector('#create').onsubmit=e=>{e.preventDefault();call('/conversations',{message:new FormData(e.target).get('request')},{headers:{'Idempotency-Key':crypto.randomUUID()}})};
const conversationId=localStorage.getItem('feed-conversation-id');if(conversationId)fetch(`/conversations/${conversationId}`).then(r=>r.json()).then(v=>{if(v.conversation_id)renderConversation(v)});"""


def _render_create_page(csrf_token):
    body = """<header><div><a href="/">‹ 返回更新</a><h1>创建关注</h1></div></header>
      <section class="card"><form id="create"><label for="request"><strong>你想持续关注什么？</strong></label>
      <textarea id="request" name="request" maxlength="2000" required placeholder="例如：关注深圳到武汉 9 月往返机票，低于 800 元提醒我。"></textarea>
      <p>可以只说主题，我会继续问。</p><button>继续</button></form>
      <div id="status" aria-live="polite"></div><div id="conversation"></div></section>"""
    return _render_shell("创建关注", body, csrf_token, CREATE_SCRIPT)


class _FirstBriefingRunner:
    """Wake the existing durable worker after commit and once at startup."""

    def __init__(self, application):
        self.application = application
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="digest-first-briefing", daemon=True,
        )
        self.thread.start()
        self.wake()

    def wake(self):
        self.wake_event.set()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        self.thread.join(5)

    def _run(self):
        while not self.stop_event.is_set():
            self.wake_event.wait()
            self.wake_event.clear()
            if self.stop_event.is_set():
                return
            while not self.stop_event.is_set():
                try:
                    result = self.application.run_outbox_once()
                except Exception:
                    break
                if result.worker_status == "NO_WORK":
                    break


class _ConditionRunner(_FirstBriefingRunner):
    """Wake the read-only fake observation worker after CONDITION commit."""

    def __init__(self, application):
        self.application = application
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="flight-condition-observation", daemon=True,
        )
        self.thread.start()
        self.wake()

    def _run(self):
        while not self.stop_event.is_set():
            self.wake_event.wait()
            self.wake_event.clear()
            if self.stop_event.is_set():
                return
            while not self.stop_event.is_set():
                try:
                    condition_results = self.application.tick_condition_observations()
                    event_results = (
                        self.application.tick_event_observations()
                        if self.application.events is not None else ()
                    )
                except Exception:
                    break
                if not condition_results and not event_results:
                    break


class DigestHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, application, config, readiness=None,
                 auto_first_briefing=True, auto_condition_observation=True):
        host, _port = address
        if host != "127.0.0.1":
            raise ValueError("Digest HTTP server must bind 127.0.0.1")
        self.application = application
        self.config = config
        self.readiness = readiness or (lambda: check_readiness(config))
        self.csrf_token = secrets.token_urlsafe(24)
        super().__init__(address, DigestHTTPRequestHandler)
        self.first_briefing_runner = None
        if auto_first_briefing and callable(
                getattr(application, "run_outbox_once", None)):
            self.first_briefing_runner = _FirstBriefingRunner(application)
        self.condition_runner = None
        if auto_condition_observation and callable(
                getattr(application, "run_condition_once", None)):
            self.condition_runner = _ConditionRunner(application)

    def request_first_briefing(self):
        if self.first_briefing_runner is not None:
            self.first_briefing_runner.wake()

    def request_condition_observation(self):
        if self.condition_runner is not None:
            self.condition_runner.wake()

    def server_close(self):
        if self.first_briefing_runner is not None:
            self.first_briefing_runner.close()
            self.first_briefing_runner = None
        if self.condition_runner is not None:
            self.condition_runner.close()
            self.condition_runner = None
        super().server_close()


class DigestHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "DigestDemo/1"

    def log_message(self, _format, *_args):
        return

    def _send(self, status, payload, content_type=JSON_TYPE, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            raise ApplicationError("invalid_request")
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            raise ApplicationError("request_too_large")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApplicationError("invalid_request") from error
        if not isinstance(value, dict):
            raise ApplicationError("invalid_request")
        return value

    @staticmethod
    def _exact(body, allowed, required=()):
        if not set(body) <= set(allowed) or not set(required) <= set(body):
            raise ApplicationError("invalid_request")

    def _csrf(self):
        if not secrets.compare_digest(
                self.headers.get("X-Digest-CSRF", ""), self.server.csrf_token):
            raise ApplicationError("csrf_failed")

    def _error(self, error):
        code = error.code if isinstance(error, ApplicationError) else "internal_error"
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "version_conflict": HTTPStatus.CONFLICT,
            "conversation_not_waiting": HTTPStatus.CONFLICT,
            "conversation_not_adjustable": HTTPStatus.CONFLICT,
            "conversation_already_committed": HTTPStatus.CONFLICT,
            "definition_not_accepted": HTTPStatus.CONFLICT,
            "idempotency_conflict": HTTPStatus.CONFLICT,
            "request_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "csrf_failed": HTTPStatus.FORBIDDEN,
        }.get(code, HTTPStatus.BAD_REQUEST if code != "internal_error" else HTTPStatus.INTERNAL_SERVER_ERROR)
        message = SAFE_FAILURE_MESSAGES.get(code, "请求未能安全完成。")
        self._send(status, {"error": {"code": code, "message": message}})

    @property
    def _user(self):
        return self.server.config.user_id

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/health":
                return self._send(HTTPStatus.OK, {"status": "alive"})
            if path == "/ready":
                report = self.server.readiness()
                status = HTTPStatus.OK if report.status == "READY" else HTTPStatus.SERVICE_UNAVAILABLE
                return self._send(status, _projection(report))
            if path == "/":
                page = _render_updates_page(
                    self.server.application, self._user,
                    self.server.csrf_token,
                )
                return self._send(HTTPStatus.OK, page, HTML_TYPE)
            if path == "/create":
                return self._send(
                    HTTPStatus.OK,
                    _render_create_page(self.server.csrf_token), HTML_TYPE,
                )
            if path == "/following":
                return self._send(
                    HTTPStatus.OK,
                    _render_following_page(
                        self.server.application, self._user,
                        self.server.csrf_token,
                    ), HTML_TYPE,
                )
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "feeds":
                return self._send(
                    HTTPStatus.OK,
                    _render_feed_page(
                        self.server.application, self._user,
                        self.server.csrf_token, parts[1],
                    ), HTML_TYPE,
                )
            if path == "/api/updates":
                value = self.server.application.get_updates_home(self._user)
            elif len(parts) == 3 and parts[:2] == ["api", "feeds"]:
                value = self.server.application.get_feed_detail(
                    self._user, parts[2],
                )
            elif path == "/subscriptions":
                value = self.server.application.list_subscriptions(self._user)
            elif (len(path.strip("/").split("/")) == 2
                  and path.startswith("/conversations/")):
                value = self.server.application.get_subscription_conversation(
                    self._user, path.split("/")[2],
                )
            elif (len(path.strip("/").split("/")) == 4
                  and path.startswith("/subscriptions/")
                  and path.endswith("/briefings/latest")):
                value = self.server.application.get_first_briefing(
                    self._user, path.split("/")[2],
                )
            elif path.startswith("/subscriptions/"):
                value = self.server.application.get_subscription(self._user, path.split("/")[2])
            elif path.startswith("/runs/"):
                value = self.server.application.get_run(self._user, path.split("/")[2])
            elif path == "/digests":
                query = parse_qs(urlsplit(self.path).query)
                value = self.server.application.list_digests(self._user, (query.get("subscription_id") or [None])[0])
            elif path.startswith("/digests/"):
                value = self.server.application.get_digest(self._user, path.split("/")[2])
            elif path == "/profile":
                value = self.server.application.get_profile(self._user)
            else:
                raise ApplicationError("not_found")
            self._send(HTTPStatus.OK, _projection(value))
        except Exception as error:
            self._error(error)

    def do_POST(self):
        start_first_briefing = False
        start_condition_observation = False
        try:
            self._csrf()
            path = urlsplit(self.path).path
            body = self._json()
            parts = path.strip("/").split("/")
            app = self.server.application
            if path == "/subscriptions":
                self._exact(body, {"request"}, {"request"})
                value = app.create_subscription(self._user, body["request"])
                status = HTTPStatus.CREATED
            elif path == "/conversations":
                self._exact(body, {"message"}, {"message"})
                key = self.headers.get("Idempotency-Key")
                if key is None:
                    raise ApplicationError("invalid_request")
                value = app.start_subscription_conversation(
                    self._user, body["message"], key,
                )
                status = HTTPStatus.OK if value.reused else HTTPStatus.CREATED
            elif (len(parts) == 3 and parts[0] == "conversations"
                  and parts[2] == "messages"):
                self._exact(body, {"message"}, {"message"})
                key = self.headers.get("Idempotency-Key")
                if key is None:
                    raise ApplicationError("invalid_request")
                value = app.continue_subscription_conversation(
                    self._user, parts[1], body["message"], key,
                )
                status = HTTPStatus.OK
            elif (len(parts) == 3 and parts[0] == "conversations"
                  and parts[2] == "adjustments"):
                self._exact(body, {"message"}, {"message"})
                key = self.headers.get("Idempotency-Key")
                if key is None:
                    raise ApplicationError("invalid_request")
                value = app.adjust_subscription_conversation(
                    self._user, parts[1], body["message"], key,
                )
                status = HTTPStatus.OK
            elif (len(parts) == 3 and parts[0] == "conversations"
                  and parts[2] == "subscription"):
                self._exact(body, set())
                value = app.commit_subscription_from_definition(
                    self._user, parts[1],
                )
                status = HTTPStatus.OK if value.reused else HTTPStatus.CREATED
                start_first_briefing = value.workflow_kind == "BRIEFING"
                start_condition_observation = value.workflow_kind in {
                    "CONDITION", "EVENT",
                }
            elif len(parts) == 3 and parts[0] == "subscriptions" and parts[2] in {"enable", "disable"}:
                self._exact(body, {"expected_version"}, {"expected_version"})
                method = app.enable_subscription if parts[2] == "enable" else app.disable_subscription
                value = method(self._user, parts[1], body["expected_version"])
                status = HTTPStatus.OK
                start_condition_observation = (
                    parts[2] == "enable"
                    and getattr(value, "workflow_kind", None)
                    in {"CONDITION", "EVENT"}
                )
            elif len(parts) == 3 and parts[0] == "subscriptions" and parts[2] == "runs":
                self._exact(body, {"period_key"})
                key = self.headers.get("Idempotency-Key")
                if key is None:
                    raise ApplicationError("invalid_request")
                value = app.run_subscription(self._user, parts[1], key, body.get("period_key"))
                status = HTTPStatus.OK
            elif len(parts) == 3 and parts[0] == "digests" and parts[2] == "deliver":
                self._exact(body, {"channel"}, {"channel"})
                value = app.deliver_digest(self._user, parts[1], body["channel"])
                status = HTTPStatus.OK
            elif len(parts) == 3 and parts[0] == "digests" and parts[2] == "feedback":
                self._exact(body, {"type", "event_key", "item_id"}, {"type", "event_key"})
                value = app.record_feedback(self._user, parts[1], body["type"], body["event_key"], body.get("item_id"))
                status = HTTPStatus.OK
            else:
                raise ApplicationError("not_found")
            self._send(status, _projection(value))
            if start_first_briefing:
                self.server.request_first_briefing()
            if start_condition_observation:
                self.server.request_condition_observation()
        except Exception as error:
            self._error(error)

    def do_PATCH(self):
        try:
            self._csrf()
            path = urlsplit(self.path).path
            parts = path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "subscriptions":
                raise ApplicationError("not_found")
            body = self._json()
            allowed = {
                "expected_version", "topic", "natural_language_request", "cadence",
                "language", "max_chars", "max_items", "focus_topics", "delivery_preference",
            }
            self._exact(body, allowed, {"expected_version"})
            expected = body.pop("expected_version")
            value = self.server.application.update_subscription(
                self._user, parts[1], expected, **body,
            )
            self._send(HTTPStatus.OK, _projection(value))
        except Exception as error:
            self._error(error)


def create_http_server(config, host="127.0.0.1", port=8765, application=None,
                       auto_first_briefing=True,
                       auto_condition_observation=True):
    """Compose the existing app once; transports never assemble services."""
    app = application or bootstrap_application(config)
    return DigestHTTPServer(
        (host, port), app, config,
        auto_first_briefing=auto_first_briefing,
        auto_condition_observation=auto_condition_observation,
    )


def main(argv=None):
    from .bootstrap import DigestAppConfig

    parser = argparse.ArgumentParser(description="Loopback AI Digest Demo")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", default=".digest-demo/digest.db")
    parser.add_argument("--workspace", default=".digest-demo/workspace")
    parser.add_argument("--audit", default=".digest-demo/audit")
    parser.add_argument("--search-provider", choices=("fake", "brave"), default="fake")
    parser.add_argument("--llm-provider", choices=("fake", "vertex"), default="fake")
    parser.add_argument("--delivery-provider", choices=("fake",), default="fake")
    args = parser.parse_args(argv)
    config = DigestAppConfig(
        args.database, args.workspace, args.audit, args.search_provider,
        args.llm_provider, args.delivery_provider,
    )
    server = create_http_server(config, args.host, args.port)
    print(f"AI Digest Demo: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
