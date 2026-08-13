import DOMPurify from 'dompurify';
import { marked } from 'marked';

const HOST_ID = 'yter-ai-summary-overlay-host';
let pollTimer = null;
let dismissed = false;

function schedulePoll(jobId) {
  clearTimeout(pollTimer);
  if (!jobId) return;
  pollTimer = setTimeout(async () => {
    let terminal = false;
    try {
      const response = await chrome.runtime.sendMessage({
        type: 'poll-yter-ai-summary',
        jobId,
      });
      if (!response?.success) {
        renderMessage({ type: 'error', text: response?.message || '查询总结状态失败' });
        terminal = true;
      } else if (['completed', 'failed'].includes(response.result?.status)) {
        terminal = true;
      }
    } catch (error) {
      renderMessage({ type: 'error', text: '暂时无法查询总结状态，将自动重试。' });
    } finally {
      if (!terminal) schedulePoll(jobId);
    }
  }, 2000);
}

function ensureOverlay() {
  let host = document.getElementById(HOST_ID);
  if (host) return host.shadowRoot;
  host = document.createElement('div');
  dismissed = false;
  host.id = HOST_ID;
  host.style.cssText = 'all:initial;position:fixed;z-index:2147483647;right:20px;top:20px;';
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      :host{all:initial}.panel{box-sizing:border-box;width:min(440px,calc(100vw - 32px));max-height:calc(100vh - 40px);overflow:auto;background:#fff;color:#172033;border:1px solid #d7ddea;border-radius:14px;box-shadow:0 16px 46px rgba(16,24,40,.24);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.header{position:sticky;top:0;display:flex;align-items:center;gap:8px;padding:12px 14px;background:#f7f9fc;border-bottom:1px solid #e5e9f2;z-index:1}.title{font-size:16px;font-weight:700;flex:1}.actions{display:flex;gap:6px}button{border:1px solid #cbd3e1;background:#fff;color:#28364f;border-radius:8px;padding:5px 9px;cursor:pointer;font:inherit}button:hover{background:#eef3fb}.status{padding:13px 14px;color:#526078}.status.error{color:#b42318}.content{padding:0 16px 16px;overflow-wrap:anywhere}.content.collapsed{max-height:320px;overflow:hidden;position:relative}.content.collapsed:after{content:"";position:absolute;left:0;right:0;bottom:0;height:70px;background:linear-gradient(transparent,#fff)}.content h1,.content h2,.content h3{line-height:1.3;margin:1em 0 .45em}.content h1{font-size:20px}.content h2{font-size:18px}.content h3{font-size:16px}.content pre{overflow:auto;padding:10px;background:#f3f5f8;border-radius:8px}.content code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.content a{color:#175cd3}.hidden{display:none!important}@media(max-width:600px){:host{right:8px!important;top:8px!important}.panel{width:calc(100vw - 16px);max-height:calc(100vh - 16px)}}
    </style>
    <section class="panel" role="dialog" aria-label="AI总结">
      <header class="header"><span class="title">AI总结</span><div class="actions"><button class="copy hidden" type="button">复制</button><button class="toggle hidden" type="button" aria-expanded="false">展开</button><button class="close" type="button" aria-label="关闭">关闭</button></div></header>
      <div class="status" role="status" aria-live="polite">正在连接 yter…</div>
      <article class="content hidden"></article>
    </section>`;
  document.documentElement.appendChild(host);

  const content = shadow.querySelector('.content');
  const status = shadow.querySelector('.status');
  const copy = shadow.querySelector('.copy');
  const toggle = shadow.querySelector('.toggle');
  shadow.querySelector('.close').addEventListener('click', () => {
    dismissed = true;
    schedulePoll(null);
    host.remove();
  });
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(content.dataset.markdown || '');
      status.textContent = '总结已复制。';
    } catch (error) {
      status.textContent = '复制失败，请手动选择内容。';
    }
  });
  toggle.addEventListener('click', () => {
    const expanded = !content.classList.toggle('collapsed');
    toggle.textContent = expanded ? '收起' : '展开';
    toggle.setAttribute('aria-expanded', String(expanded));
  });
  return shadow;
}

function renderMessage(message) {
  if (dismissed) return;
  const shadow = ensureOverlay();
  const status = shadow.querySelector('.status');
  const content = shadow.querySelector('.content');
  const copy = shadow.querySelector('.copy');
  const toggle = shadow.querySelector('.toggle');
  status.classList.toggle('error', message.type === 'error');
  if (message.type === 'streaming') {
    const markdown = message.markdown || '';
    content.innerHTML = DOMPurify.sanitize(
      marked.parse(markdown, { gfm: true, breaks: true }),
      {
        FORBID_TAGS: ['img', 'svg', 'math', 'style', 'script'],
        FORBID_ATTR: ['style'],
      },
    );
    content.dataset.markdown = markdown;
    content.classList.remove('hidden', 'collapsed');
    copy.classList.add('hidden');
    toggle.classList.add('hidden');
    status.textContent = message.text || 'AI 正在流式生成总结…';
    return;
  }
  if (message.type === 'completed') {
    schedulePoll(null);
    const markdown = message.summary?.markdown || '';
    content.innerHTML = DOMPurify.sanitize(
      marked.parse(markdown, { gfm: true, breaks: true }),
      {
        FORBID_TAGS: ['img', 'svg', 'math', 'style', 'script'],
        FORBID_ATTR: ['style'],
      },
    );
    content.dataset.markdown = markdown;
    content.classList.remove('hidden');
    content.classList.add('collapsed');
    copy.classList.remove('hidden');
    toggle.classList.remove('hidden');
    toggle.textContent = '展开';
    toggle.setAttribute('aria-expanded', 'false');
    status.textContent = message.cached ? '已读取保存的总结。' : 'AI 总结已生成并保存。';
    return;
  }
  if (message.type === 'error') schedulePoll(null);
  if (message.type === 'pending' && message.jobId) schedulePoll(message.jobId);
  content.classList.add('hidden');
  copy.classList.add('hidden');
  toggle.classList.add('hidden');
  status.textContent = message.text || '正在处理…';
}

globalThis.__yterAiSummaryRender = renderMessage;
if (!globalThis.__yterAiSummaryListenerInstalled) {
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.channel === 'yter-ai-summary') {
      globalThis.__yterAiSummaryRender(message);
    }
  });
  globalThis.__yterAiSummaryListenerInstalled = true;
}

ensureOverlay();
