/**
 * useAgentChat.js — 前端状态机化（阶段5）。
 *
 * 将 App.jsx 的 sendMessage 流式处理、fallback 进度、异常兜底整体迁入本 hook，
 * 并把执行过程建模为状态机：
 *   idle → sending → streaming → awaiting_confirmation | awaiting_input
 *        → done | error | cancelled
 *   （streaming 中断时进入 reconnecting，经 REST 补拉事件后恢复 streaming）
 *
 * 与后端 RunEngine 契约对齐：
 *   - POST /chat/stream（SSE）：事件 type ∈ status/intent/plan/tool_start/tool_result/
 *     verification/pending/final/error/cancelled；pending 与 final 成对出现；
 *     resume 时携带 X-Pending-Run-ID 请求头
 *   - GET  /api/run/{id}                    查询 run 状态（含 pending 载荷）
 *   - GET  /api/run/{id}/events?since=seq   断线补拉（seq 递增、事件持久化）
 *   - POST /api/run/{id}/cancel             取消（步骤间停止）
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export const AGENT_PHASE = {
  IDLE: 'idle',
  SENDING: 'sending',
  STREAMING: 'streaming',
  RECONNECTING: 'reconnecting',
  AWAITING_CONFIRMATION: 'awaiting_confirmation',
  AWAITING_INPUT: 'awaiting_input',
  DONE: 'done',
  ERROR: 'error',
  CANCELLED: 'cancelled',
};

const BUSY_PHASES = new Set([
  AGENT_PHASE.SENDING,
  AGENT_PHASE.STREAMING,
  AGENT_PHASE.RECONNECTING,
]);
const TERMINAL_PHASES = new Set([
  AGENT_PHASE.DONE,
  AGENT_PHASE.ERROR,
  AGENT_PHASE.CANCELLED,
]);
const TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled']);

const RECONNECT_POLL_MS = 1500;
const RECONNECT_MAX_MS = 10 * 60 * 1000; // 对齐后端 run 超时（10 分钟）

// 事件类型 → 进度卡片色调（沿用 App.jsx toneMap 并扩展新事件类型）
const TONE_MAP = {
  error: 'error',
  tool_result: 'success',
  intent: 'info',
  plan: 'info',
  tool_start: 'info',
  status: 'info',
  verification: 'warning',
  pending: 'warning',
  cancelled: 'error',
};

const SKIP_STAGES = new Set(['queued', 'start']);

const FALLBACK_STEPS = [
  '后端连接已建立，正在准备分析上下文。',
  '正在等待后端返回第一条真实执行日志。',
  '如果任务涉及数据库、地图或图表，真实步骤会继续显示在这里。',
  '任务仍在执行中，结果生成后会立刻展示到这里。',
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export default function useAgentChat({ onFinal, onAssistantMessage, onConnectionError } = {}) {
  const [phase, setPhase] = useState(AGENT_PHASE.IDLE);
  const [runId, setRunId] = useState(null);
  const [pendingInfo, setPendingInfo] = useState(null);
  const [progress, setProgress] = useState([]);
  const [agentError, setAgentError] = useState('');

  const phaseRef = useRef(AGENT_PHASE.IDLE);
  const runIdRef = useRef(null);
  const pendingRef = useRef(null);
  const seqRef = useRef(0);

  // 进度 / fallback refs（自 App.jsx 迁移）
  const progressSeqRef = useRef(0);
  const hasActualEventRef = useRef(false);
  const fallbackTimerRef = useRef(null);
  const fallbackIndexRef = useRef(0);
  const lastServerEventAtRef = useRef(0);

  // 外部回调 ref（避免 useCallback 依赖抖动）
  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;
  const onAssistantMessageRef = useRef(onAssistantMessage);
  onAssistantMessageRef.current = onAssistantMessage;
  const onConnectionErrorRef = useRef(onConnectionError);
  onConnectionErrorRef.current = onConnectionError;

  const setPhaseBoth = useCallback((p) => {
    phaseRef.current = p;
    setPhase(p);
  }, []);

  const setRunIdBoth = useCallback((id) => {
    runIdRef.current = id;
    setRunId(id);
  }, []);

  const setPendingBoth = useCallback((info) => {
    pendingRef.current = info;
    setPendingInfo(info);
  }, []);

  // ------------------------------------------------------------------
  // 进度管理（自 App.jsx pushStreamProgress 等逐行迁移）
  // ------------------------------------------------------------------

  const pushProgress = useCallback((entryOrText, tone = 'info') => {
    const normalized = typeof entryOrText === 'string'
      ? { title: entryOrText, text: entryOrText, tone }
      : (entryOrText || {});

    const title = normalized.title || normalized.text || '正在处理中...';
    const text = normalized.text || title;
    const details = Array.isArray(normalized.details)
      ? normalized.details.filter(Boolean).slice(0, 6)
      : [];

    progressSeqRef.current += 1;
    setProgress(prev => [...prev, {
      id: progressSeqRef.current,
      title,
      text,
      tone: normalized.tone || tone,
      details,
      badge: normalized.badge || '',
      stage: normalized.stage || '',
      source: normalized.source || 'server',
    }]);
  }, []);

  const resetProgress = useCallback(() => {
    progressSeqRef.current = 0;
    hasActualEventRef.current = false;
    setProgress([]);
  }, []);

  const noteServerProgress = useCallback(() => {
    lastServerEventAtRef.current = Date.now();
  }, []);

  const stopFallbackProgress = useCallback(() => {
    if (fallbackTimerRef.current) {
      window.clearInterval(fallbackTimerRef.current);
      fallbackTimerRef.current = null;
    }
    fallbackIndexRef.current = 0;
  }, []);

  const activateRealProgress = useCallback(() => {
    noteServerProgress();
    stopFallbackProgress();
    if (!hasActualEventRef.current) {
      hasActualEventRef.current = true;
      // 首次真实事件到达：清除兜底与重连提示条目
      setProgress(prev => prev.filter(
        item => item.source !== 'fallback' && item.source !== 'reconnect'
      ));
    }
    if (phaseRef.current === AGENT_PHASE.SENDING || phaseRef.current === AGENT_PHASE.RECONNECTING) {
      setPhaseBoth(AGENT_PHASE.STREAMING);
    }
  }, [noteServerProgress, setPhaseBoth, stopFallbackProgress]);

  const startFallbackProgress = useCallback(() => {
    stopFallbackProgress();
    lastServerEventAtRef.current = Date.now();

    fallbackTimerRef.current = window.setInterval(() => {
      if (Date.now() - lastServerEventAtRef.current < 3500) {
        return;
      }

      const nextText = FALLBACK_STEPS[Math.min(fallbackIndexRef.current, FALLBACK_STEPS.length - 1)];
      setProgress(prev => {
        if (prev.some(item => item.title === nextText && item.source === 'fallback')) return prev;
        progressSeqRef.current += 1;
        return [...prev, {
          id: progressSeqRef.current,
          title: nextText,
          text: nextText,
          tone: 'info',
          details: ['当前还没收到后端的第一条真实步骤事件。'],
          badge: '兜底提示',
          stage: 'fallback',
          source: 'fallback',
        }];
      });

      if (fallbackIndexRef.current < FALLBACK_STEPS.length - 1) {
        fallbackIndexRef.current += 1;
      }
      lastServerEventAtRef.current = Date.now();
    }, 4000);
  }, [stopFallbackProgress]);

  useEffect(() => () => stopFallbackProgress(), [stopFallbackProgress]);

  // ------------------------------------------------------------------
  // 错误映射（自 App.jsx catch 块逐行迁移）
  // ------------------------------------------------------------------

  const mapErrorMessage = useCallback((error) => {
    let errorMessage = '抱歉，发生了错误。请稍后重试。';
    if (error.message.includes('Failed to fetch') || error.message.includes('ERR_CONNECTION_REFUSED')) {
      errorMessage = '无法连接到服务器，请确保后端服务正在运行 (http://localhost:8006)';
      onConnectionErrorRef.current?.(true);
    } else if (error.message.includes('Messages cannot be empty')) {
      errorMessage = '请输入消息内容。';
    } else if (error.message.includes('Invalid role')) {
      errorMessage = '消息格式错误。';
    } else if (error.message.includes('Network')) {
      errorMessage = '网络连接错误，请检查网络连接。';
    } else if (error.message.includes('未收到最终结果')) {
      errorMessage = '后端流式处理中断，未返回最终结果。';
    } else if (error.message.includes('任务记录不存在')) {
      errorMessage = '任务记录已失效，请重新发起请求。';
    }
    return errorMessage;
  }, []);

  // ------------------------------------------------------------------
  // 事件处理
  // ------------------------------------------------------------------

  const handleFinalResult = useCallback((result) => {
    const res = result || {};
    if (res.run_id) setRunIdBoth(res.run_id);

    // pending 终结：pending 与 final 成对出现，final 携带完整 missing / intent_info
    if (res.pending_type) {
      const prev = pendingRef.current || {};
      setPendingBoth({
        run_id: res.run_id || prev.run_id || runIdRef.current,
        type: res.pending_type,
        question: res.response || prev.question || '',
        plan: res.intent_info || prev.plan || null,
        missing: Array.isArray(res.missing) && res.missing.length > 0
          ? res.missing
          : (prev.missing || []),
      });
      setPhaseBoth(
        res.pending_type === 'confirm'
          ? AGENT_PHASE.AWAITING_CONFIRMATION
          : AGENT_PHASE.AWAITING_INPUT
      );
      return;
    }

    if (res.success === false) {
      setAgentError(res.response || '任务执行失败。');
      setPhaseBoth(AGENT_PHASE.ERROR);
    } else {
      setPhaseBoth(AGENT_PHASE.DONE);
    }
    // 最终结果已到：清除可能残留的 pending 信息（如 resume 时回放的旧 pending 事件）
    setPendingBoth(null);
    onFinalRef.current?.(res);
  }, [setPendingBoth, setPhaseBoth, setRunIdBoth]);

  const handleEvent = useCallback((event) => {
    if (!event || typeof event !== 'object') return;
    if (typeof event.seq === 'number' && event.seq > seqRef.current) {
      seqRef.current = event.seq;
    }
    if (event.run_id && event.run_id !== runIdRef.current) {
      setRunIdBoth(event.run_id);
    }

    if (event.type === 'pending') {
      // pending 事件先切状态机（缺参详情以随后的 final 为准）
      setPendingBoth({
        run_id: event.run_id || runIdRef.current,
        type: event.pending_type || 'input',
        question: event.message || '',
        plan: null,
        missing: Array.isArray(event.details)
          ? event.details.filter(Boolean).map(label => ({ param: label, label }))
          : [],
      });
      setPhaseBoth(
        event.pending_type === 'confirm'
          ? AGENT_PHASE.AWAITING_CONFIRMATION
          : AGENT_PHASE.AWAITING_INPUT
      );
      return;
    }

    if (event.type === 'cancelled') {
      // 用户主动取消：cancelRun 已追加提示消息，这里只收尾状态
      setPhaseBoth(AGENT_PHASE.CANCELLED);
    }

    if (SKIP_STAGES.has(event.stage)) return;

    activateRealProgress();
    pushProgress({
      title: event.title || event.message || event.type,
      text: event.message || event.title || '',
      details: Array.isArray(event.details) ? event.details : [],
      tone: TONE_MAP[event.type] || 'info',
      stage: event.stage || event.type,
      badge: event.tool_label
        || (event.type === 'verification' ? `校验:${event.tool_name || ''}` : event.stage)
        || '',
    });
  }, [activateRealProgress, pushProgress, setPhaseBoth, setPendingBoth, setRunIdBoth]);

  // ------------------------------------------------------------------
  // SSE 消费
  // ------------------------------------------------------------------

  const consumeStream = useCallback(async (body) => {
    const reader = body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let gotFinal = false;

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const normalizedBuffer = buffer.replace(/\r\n/g, '\n');

      const chunks = normalizedBuffer.split('\n\n');
      buffer = chunks.pop() || '';

      chunks.forEach(chunk => {
        const lines = chunk.split('\n').filter(Boolean);
        lines.forEach(line => {
          if (!line.startsWith('data:')) return;
          const payload = line.slice(5).trim();
          if (!payload || payload === '[DONE]') return;

          try {
            const event = JSON.parse(payload);
            if (event.type === 'final') {
              gotFinal = true;
              handleFinalResult(event.result);
            } else {
              handleEvent(event);
            }
          } catch (parseError) {
            console.warn('解析流式事件失败:', parseError, payload);
          }
        });
      });

      if (done) break;
    }

    // 无 final 且非用户取消：视为流中断（网络断开 / 服务重启），由上层决定补拉
    if (!gotFinal && phaseRef.current !== AGENT_PHASE.CANCELLED) {
      throw new Error('未收到最终结果。');
    }
  }, [handleEvent, handleFinalResult]);

  // ------------------------------------------------------------------
  // 断线重连：GET /api/run/{id} 查状态 + GET .../events?since=seq 补拉
  // ------------------------------------------------------------------

  const reconnectAndDrain = useCallback(async () => {
    const rid = runIdRef.current;
    if (!rid) return;

    setPhaseBoth(AGENT_PHASE.RECONNECTING);
    stopFallbackProgress();
    pushProgress({
      title: '连接已断开，正在重连并补拉事件……',
      text: 'run 仍在后端独立执行，重连后会自动恢复进度显示。',
      tone: 'warning',
      badge: '重连中',
      stage: 'reconnecting',
      source: 'reconnect',
    });

    const startedAt = Date.now();
    try {
      while (true) {
        if (Date.now() - startedAt > RECONNECT_MAX_MS) {
          throw new Error('重连补拉超时，未收到最终结果。');
        }

        // 1. 查 run 状态
        let info = null;
        try {
          const statusRes = await fetch(`/api/run/${encodeURIComponent(rid)}`);
          if (statusRes.ok) {
            info = await statusRes.json();
          } else if (statusRes.status === 404) {
            throw new Error('任务记录不存在，无法恢复执行状态。');
          }
        } catch (e) {
          if (e.message && e.message.includes('任务记录不存在')) throw e;
          await sleep(RECONNECT_POLL_MS); // 网络暂断，稍后重试
          continue;
        }

        // 2. 补拉 seq 之后的事件
        try {
          const eventsRes = await fetch(`/api/run/${encodeURIComponent(rid)}/events?since=${seqRef.current}`);
          if (eventsRes.ok) {
            const data = await eventsRes.json();
            const events = data.events || [];
            for (const ev of events) {
              if (ev.type === 'final') {
                handleFinalResult(ev.result);
                return;
              }
              handleEvent(ev);
            }
            if (typeof data.latest_seq === 'number' && data.latest_seq > seqRef.current) {
              seqRef.current = data.latest_seq;
            }
          }
        } catch (e) {
          console.warn('补拉事件失败，稍后重试:', e);
          await sleep(RECONNECT_POLL_MS);
          continue;
        }

        // 3. 依据状态推进状态机
        const status = info.status;
        if (status === 'awaiting_confirmation' || status === 'awaiting_input') {
          // run 停在 pending：从 run 载荷恢复确认/补参卡片
          const pending = info.pending || {};
          setPendingBoth({
            run_id: rid,
            type: pending.pending_type || (status === 'awaiting_confirmation' ? 'confirm' : 'input'),
            question: pending.question || '',
            plan: pending.plan || null,
            missing: pending.missing || [],
          });
          setPhaseBoth(
            status === 'awaiting_confirmation'
              ? AGENT_PHASE.AWAITING_CONFIRMATION
              : AGENT_PHASE.AWAITING_INPUT
          );
          return;
        }

        if (TERMINAL_RUN_STATUSES.has(status)) {
          // 终态但未补拉到 final 事件（事件可能已被清理）
          if (status === 'cancelled') {
            setPhaseBoth(AGENT_PHASE.CANCELLED);
          } else {
            const errorMessage = '任务已结束，但未收到最终结果。';
            onAssistantMessageRef.current?.(errorMessage);
            pushProgress(errorMessage, 'error');
            setAgentError(errorMessage);
            setPhaseBoth(AGENT_PHASE.ERROR);
          }
          return;
        }

        // 仍在执行：等待后继续补拉
        await sleep(RECONNECT_POLL_MS);
      }
    } catch (error) {
      const errorMessage = mapErrorMessage(error);
      onAssistantMessageRef.current?.(errorMessage);
      pushProgress(errorMessage, 'error');
      setAgentError(errorMessage);
      setPhaseBoth(AGENT_PHASE.ERROR);
    }
  }, [handleEvent, handleFinalResult, mapErrorMessage, pushProgress, setAgentError, setPendingBoth, setPhaseBoth, stopFallbackProgress]);

  // ------------------------------------------------------------------
  // 发送入口
  // ------------------------------------------------------------------

  const send = useCallback(async ({ messages, activeView, sessionId, pendingRunId } = {}) => {
    if (BUSY_PHASES.has(phaseRef.current)) return;

    setAgentError('');
    setPhaseBoth(AGENT_PHASE.SENDING);
    setRunIdBoth(pendingRunId || null);
    if (pendingRunId) setPendingBoth(null); // resume：清除旧 pending 卡片
    resetProgress();
    startFallbackProgress();

    try {
      const headers = {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
      };
      if (pendingRunId) headers['X-Pending-Run-ID'] = pendingRunId;

      const response = await fetch(`/chat/stream`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          messages,
          active_view: activeView,
          session_id: sessionId,
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        const errorMessage = errorData.detail || `Server error: ${response.status}`;
        throw new Error(errorMessage);
      }

      if (!response.body) {
        throw new Error('浏览器不支持流式响应。');
      }

      await consumeStream(response.body);
    } catch (error) {
      console.error('Error:', error);
      if (phaseRef.current === AGENT_PHASE.CANCELLED) {
        return; // 用户已取消：流断开无需报错
      }
      const rid = runIdRef.current;
      if (rid && !TERMINAL_PHASES.has(phaseRef.current)) {
        // 已有 run 且未终态：断线重连补拉（run 在后端独立执行，不受 SSE 断开影响）
        await reconnectAndDrain();
      } else {
        const errorMessage = mapErrorMessage(error);
        onAssistantMessageRef.current?.(errorMessage);
        pushProgress(errorMessage, 'error');
        setAgentError(errorMessage);
        setPhaseBoth(AGENT_PHASE.ERROR);
      }
    } finally {
      stopFallbackProgress();
    }
  }, [consumeStream, mapErrorMessage, pushProgress, reconnectAndDrain, resetProgress, setAgentError, setPendingBoth, setPhaseBoth, setRunIdBoth, startFallbackProgress, stopFallbackProgress]);

  // ------------------------------------------------------------------
  // 取消
  // ------------------------------------------------------------------

  const cancelRun = useCallback(async () => {
    const rid = runIdRef.current;
    if (!rid) return false;
    try {
      const res = await fetch(`/api/run/${encodeURIComponent(rid)}/cancel`, { method: 'POST' });
      if (!res.ok) throw new Error(`cancel failed: ${res.status}`);
      const data = await res.json();
      if (data.cancelled) {
        setPhaseBoth(AGENT_PHASE.CANCELLED);
        stopFallbackProgress();
        onAssistantMessageRef.current?.('任务已取消。');
        pushProgress({
          title: '取消请求已提交',
          text: data.message || '取消标记已设置，run 将在当前步骤完成后停止。',
          tone: 'warning',
          badge: '已取消',
          stage: 'cancel',
        });
      }
      return true;
    } catch (e) {
      console.warn('取消任务失败:', e);
      pushProgress('取消任务失败，请重试。', 'error');
      return false;
    }
  }, [pushProgress, setPhaseBoth, stopFallbackProgress]);

  // ------------------------------------------------------------------
  // 复位
  // ------------------------------------------------------------------

  const reset = useCallback(() => {
    resetProgress();
    stopFallbackProgress();
    seqRef.current = 0;
    setPendingBoth(null);
    setRunIdBoth(null);
    setAgentError('');
    setPhaseBoth(AGENT_PHASE.IDLE);
  }, [resetProgress, setAgentError, setPendingBoth, setPhaseBoth, setRunIdBoth, stopFallbackProgress]);

  const isBusy = BUSY_PHASES.has(phase);

  return {
    phase,
    runId,
    pendingInfo,
    progress,
    agentError,
    isBusy,
    send,
    cancelRun,
    reset,
  };
}
