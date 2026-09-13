'use client'

import { useState } from 'react'
import { ArrowDownToLine, Check, Clock3, Download, FileAudio, FileVideo, Link2, LogIn, Plus, ShieldCheck, Sparkles, TerminalSquare, X } from 'lucide-react'
import { authClient } from '@/lib/auth-client'
import { createTask } from './actions/tasks'

type Task = { id: number; url: string; formats: string; status: string; createdAt: Date; logs: string[] }

const demoTasks: Task[] = [
  { id: 1, url: 'https://youtube.com/watch?v=design-talk', formats: '视频 MP4', status: 'completed', createdAt: new Date(), logs: ['[debug] Extracting URL: https://youtube.com/watch?v=design-talk', '[youtube] design-talk: Downloading webpage', '[info] Destination: design-talk.mp4', '[download] 100% of 24.18MiB in 00:18'] },
  { id: 2, url: 'https://vimeo.com/creative-session', formats: '音频 MP3', status: 'downloading', createdAt: new Date(), logs: ['[debug] Extracting URL: https://vimeo.com/creative-session', '[vimeo] Downloading config JSON', '[info] Requested format: bestaudio', '[download] 42.7% of 8.04MiB at 1.24MiB/s ETA 00:04'] },
]

export default function Page() {
  const [url, setUrl] = useState('')
  const [video, setVideo] = useState(true)
  const [audio, setAudio] = useState(false)
  const [tasks, setTasks] = useState<Task[]>(demoTasks)
  const [authOpen, setAuthOpen] = useState(false)
  const [isSignUp, setIsSignUp] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [selectedTask, setSelectedTask] = useState<Task | null>(null)

  async function submitTask() {
    if (!url.trim() || (!video && !audio)) return
    const formats = [video && '视频 MP4', audio && '音频 MP3'].filter(Boolean) as string[]
    const newTask: Task = { id: Date.now(), url: url.trim(), formats: formats.join(' / '), status: 'queued', createdAt: new Date(), logs: ['[debug] Task created', `[debug] URL: ${url.trim()}`, `[debug] Requested formats: ${formats.join(', ')}`, '[info] Waiting for downloader service...'] }
    setTasks((current) => [newTask, ...current])
    setUrl('')
    try { await createTask(newTask.url, formats) } catch { /* 未登录时保留前端演示 */ }
  }

  async function submitAuth() {
    setSubmitting(true)
    setError('')
    const result = isSignUp ? await authClient.signUp.email({ email, password, name }) : await authClient.signIn.email({ email, password })
    if (result.error) setError('邮箱或密码不正确，请检查后重试。')
    else { setAuthOpen(false); setEmail(''); setPassword(''); setName('') }
    setSubmitting(false)
  }

  return (
    <main className="min-h-screen bg-[#f4f7fb] text-[#172033]">
      <header className="border-b border-[#e3e9f2] bg-white">
        <div className="mx-auto flex h-[58px] max-w-[1320px] items-center justify-between px-3.5 sm:h-[68px] sm:px-5 lg:px-8">
          <div className="flex items-center gap-3"><div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#1769e0] text-white shadow-sm"><ArrowDownToLine size={21} /></div><span className="text-[19px] font-bold tracking-tight">Drop<span className="text-[#1769e0]">Load</span></span></div>
          <button onClick={() => setAuthOpen(true)} className="flex items-center gap-2 rounded-lg border border-[#dce4ef] px-4 py-2 text-sm font-medium text-[#445269] transition hover:border-[#1769e0] hover:text-[#1769e0]"><LogIn size={16} /> 登录 / 注册</button>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1320px] gap-3 px-3 py-3 sm:gap-6 sm:px-5 sm:py-6 lg:grid-cols-[minmax(0,1fr)_390px] lg:px-8">
        <section className="rounded-2xl border border-[#e2e8f0] bg-white p-4 shadow-[0_8px_30px_rgba(42,72,112,0.05)] sm:min-h-[620px] sm:p-6 lg:p-10">
          <div className="flex flex-col justify-center sm:min-h-[540px]">
            <div className="mb-4 hidden w-fit items-center gap-2 rounded-full bg-[#e8f1ff] px-3 py-1.5 text-xs font-semibold text-[#1769e0] sm:flex"><Sparkles size={14} /> 简单、快速、无广告</div>
            <h1 className="max-w-[620px] text-[30px] font-bold tracking-[-0.04em] text-[#14213d] sm:text-4xl lg:text-5xl">把链接变成文件</h1>
            <p className="mt-2 max-w-[600px] text-sm leading-6 text-[#718096] sm:mt-4 sm:text-[16px] sm:leading-7">粘贴来自 YouTube、Vimeo 或其他网站的视频链接，选择需要的格式，任务会自动加入右侧列表。</p>
            <div className="mt-6 max-w-[760px] rounded-2xl border border-[#e1e8f1] bg-[#fbfcfe] p-2.5 shadow-[0_12px_40px_rgba(40,73,120,0.08)] sm:mt-9 sm:p-3">
              <div className="flex flex-col gap-2 rounded-xl border border-[#dce4ef] bg-white p-2 focus-within:border-[#1769e0] focus-within:ring-4 focus-within:ring-[#1769e0]/10 sm:flex-row sm:items-center sm:px-4 sm:py-1.5"><Link2 size={19} className="hidden shrink-0 text-[#91a0b5] sm:block" /><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="粘贴视频或音频链接..." className="min-w-0 flex-1 bg-transparent px-2 py-2.5 text-[15px] outline-none placeholder:text-[#9aa8ba] sm:px-0 sm:py-3" /><button onClick={submitTask} disabled={!url.trim() || (!video && !audio)} className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#1769e0] px-5 py-3 text-sm font-semibold text-white transition hover:bg-[#0f58c4] disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto"><Plus size={16} /> 加入队列</button></div>
              <div className="mt-3 flex flex-nowrap items-center gap-x-3 overflow-x-auto px-2 text-sm text-[#65748a] sm:gap-5"><span className="mr-1 shrink-0 text-xs font-semibold uppercase tracking-wider text-[#9aa8ba]">下载格式</span><label className="flex shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap sm:gap-2"><input type="checkbox" checked={video} onChange={(e) => setVideo(e.target.checked)} className="peer sr-only" /><span className="flex h-4 w-4 shrink-0 items-center justify-center rounded border border-[#b8c5d6] text-transparent peer-checked:border-[#1769e0] peer-checked:bg-[#1769e0] peer-checked:text-white"><Check size={12} /></span><FileVideo size={16} /> 视频 MP4</label><label className="flex shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap sm:gap-2"><input type="checkbox" checked={audio} onChange={(e) => setAudio(e.target.checked)} className="peer sr-only" /><span className="flex h-4 w-4 shrink-0 items-center justify-center rounded border border-[#b8c5d6] text-transparent peer-checked:border-[#1769e0] peer-checked:bg-[#1769e0] peer-checked:text-white"><Check size={12} /></span><FileAudio size={16} /> 音频 MP3</label></div>
            </div>
            <div className="mt-6 flex items-center gap-2 text-xs text-[#9aa8ba]"><ShieldCheck size={14} /> 链接仅用于处理任务，我们尊重你的隐私</div>
          </div>
        </section>

        <aside className="flex max-h-[420px] flex-col overflow-hidden rounded-2xl border border-[#e2e8f0] bg-white shadow-[0_8px_30px_rgba(42,72,112,0.05)] sm:min-h-[620px] sm:max-h-none">
          <div className="border-b border-[#edf1f5] px-5 py-5"><div className="flex items-center justify-between"><div><h2 className="text-lg font-bold">任务列表</h2><p className="mt-1 text-sm text-[#8794a7]">下载任务会显示在这里</p></div><span className="rounded-full bg-[#e8f1ff] px-3 py-1 text-xs font-semibold text-[#1769e0]">{tasks.length} 个任务</span></div></div>
          <div className="flex-1 overflow-y-auto">{tasks.map((task) => (<div role="button" tabIndex={0} key={task.id} onClick={() => setSelectedTask(task)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') setSelectedTask(task) }} className="flex w-full cursor-pointer items-start gap-3 border-b border-[#edf1f5] px-5 py-4 text-left transition hover:bg-[#f8fbff] last:border-0"><div className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${task.status === 'completed' ? 'bg-[#e9f8ef] text-[#24a05a]' : task.status === 'downloading' ? 'bg-[#fff6df] text-[#d18a00]' : 'bg-[#edf3ff] text-[#1769e0]'}`}>{task.status === 'completed' ? <Check size={17} /> : task.status === 'downloading' ? <Clock3 size={17} /> : <Download size={17} />}</div><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium text-[#334155]" title={task.url}>{task.url}</p><div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-[#97a3b3]"><span>{task.formats}</span><span>·</span><span>{task.status === 'completed' ? '已完成' : task.status === 'downloading' ? '处理中...' : '等待处理'}</span></div>{task.status === 'completed' && <span className="mt-2 flex items-center gap-1.5 text-xs font-semibold text-[#1769e0]"><Download size={13} /> 下载文件</span>}</div></div>))}</div>
        </aside>

        {selectedTask && <div className="fixed inset-0 z-40 bg-[#13213b]/20" onClick={() => setSelectedTask(null)}><section role="dialog" aria-label="任务详情日志" className="absolute inset-y-0 right-0 flex w-full max-w-[460px] flex-col border-l border-[#dfe7f1] bg-white shadow-2xl" onClick={(event) => event.stopPropagation()}><div className="flex items-center justify-between border-b border-[#edf1f5] px-5 py-5"><div><div className="flex items-center gap-2 text-[#1769e0]"><TerminalSquare size={17} /><span className="text-xs font-semibold uppercase tracking-wider">任务详情</span></div><h2 className="mt-2 max-w-[330px] truncate text-base font-bold text-[#172033]">{selectedTask.url}</h2></div><button type="button" aria-label="关闭任务详情" onClick={() => setSelectedTask(null)} className="rounded-lg p-2 text-[#8794a7] transition hover:bg-[#f1f5fa] hover:text-[#172033]"><X size={20} /></button></div><div className="border-b border-[#edf1f5] px-5 py-4"><div className="flex items-center justify-between text-xs text-[#8794a7]"><span>{selectedTask.formats}</span><span className="font-semibold text-[#1769e0]">{selectedTask.status === 'completed' ? '已完成' : selectedTask.status === 'downloading' ? '处理中...' : '等待处理'}</span></div></div><div className="flex-1 overflow-y-auto bg-[#101827] p-5 font-mono text-[12px] leading-6 text-[#b9c7d9]">{selectedTask.logs.map((log, index) => <p key={`${selectedTask.id}-${index}`} className="break-words"><span className="mr-3 select-none text-[#53647b]">{String(index + 1).padStart(2, '0')}</span>{log}</p>)}</div></section></div>}
      </div>

      {authOpen && <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#13213b]/30 p-4" onClick={() => setAuthOpen(false)}><div className="w-full max-w-[400px] rounded-2xl bg-white p-7 shadow-2xl" onClick={(e) => e.stopPropagation()}><div className="mb-6 flex items-start justify-between"><div><h2 className="text-xl font-bold">{isSignUp ? '创建你的账号' : '欢迎回来'}</h2><p className="mt-1 text-sm text-[#8794a7]">{isSignUp ? '保存并管理你的下载任务' : '登录后查看你的任务记录'}</p></div><button onClick={() => setAuthOpen(false)} className="text-[#95a1b2] hover:text-[#172033]"><X size={20} /></button></div><div className="space-y-3">{isSignUp && <input value={name} onChange={(e) => setName(e.target.value)} placeholder="你的昵称" className="w-full rounded-lg border border-[#dce4ef] px-3 py-3 text-sm outline-none focus:border-[#1769e0]" />}<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="邮箱地址" className="w-full rounded-lg border border-[#dce4ef] px-3 py-3 text-sm outline-none focus:border-[#1769e0]" /><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="密码（至少 8 位）" className="w-full rounded-lg border border-[#dce4ef] px-3 py-3 text-sm outline-none focus:border-[#1769e0]" />{error && <p className="text-sm text-red-500">{error}</p>}<button onClick={submitAuth} disabled={submitting || !email || !password || (isSignUp && !name)} className="w-full rounded-lg bg-[#1769e0] py-3 text-sm font-semibold text-white hover:bg-[#0f58c4] disabled:opacity-40">{submitting ? '处理中...' : isSignUp ? '注册账号' : '登录'}</button></div><button onClick={() => { setIsSignUp(!isSignUp); setError('') }} className="mt-5 w-full text-center text-sm text-[#1769e0]">{isSignUp ? '已有账号？立即登录' : '还没有账号？注册一个'}</button></div></div>}
    </main>
  )
}
  
