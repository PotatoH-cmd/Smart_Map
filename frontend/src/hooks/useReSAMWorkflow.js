import { useState, useCallback, useRef, useEffect } from 'react';

/**
 * useReSAMWorkflow — ReSAM 全循环工作流状态机
 *
 * 串联标注 → 训练 → 推理的完整闭环：
 *
 *   IDLE → DRAW → SAM_DETECT → ANNOTATE → REQUERY → TRAIN → COMPLETE
 *     ↑                                                        |
 *     └──────────── 新一轮（精度提升后重新检测）←───────────────┘
 *
 * 用法:
 *   const workflow = useReSAMWorkflow();
 *   workflow.startDraw();         // 进入绘制阶段
 *   workflow.startDetect();       // SAM 检测
 *   workflow.startAnnotate();     // 标注修正
 *   workflow.startRequery();      // 精修
 *   workflow.submitTrain();       // 提交训练
 *   workflow.reset();             // 重置
 */

const PHASES = {
  IDLE: 'IDLE',
  DRAW: 'DRAW',
  SAM_DETECT: 'SAM_DETECT',
  ANNOTATE: 'ANNOTATE',
  REQUERY: 'REQUERY',
  TRAIN: 'TRAIN',
  COMPLETE: 'COMPLETE',
};

const PHASE_ORDER = [
  PHASES.IDLE,
  PHASES.DRAW,
  PHASES.SAM_DETECT,
  PHASES.ANNOTATE,
  PHASES.REQUERY,
  PHASES.TRAIN,
  PHASES.COMPLETE,
];

const PHASE_LABELS = {
  [PHASES.IDLE]: '空闲',
  [PHASES.DRAW]: '绘制区域',
  [PHASES.SAM_DETECT]: 'SAM 检测',
  [PHASES.ANNOTATE]: '标注修正',
  [PHASES.REQUERY]: '精修复审',
  [PHASES.TRAIN]: 'SSA 微调',
  [PHASES.COMPLETE]: '完成',
};

const ANNOTATION_THRESHOLD = 50;  // 累积 ≥ 50 条方可触发训练

export function useReSAMWorkflow() {
  const [phase, setPhase] = useState(PHASES.IDLE);
  const [iteration, setIteration] = useState(0);
  const [sessionId, setSessionId] = useState('');
  const [detectResult, setDetectResult] = useState(null);
  const [annotationCount, setAnnotationCount] = useState(0);
  const [trainTaskId, setTrainTaskId] = useState('');
  const [trainProgress, setTrainProgress] = useState(null);
  const [error, setError] = useState(null);

  const phaseIndex = PHASE_ORDER.indexOf(phase);
  const pollRef = useRef(null);

  // 清理轮询
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  // ── 阶段转换 ──

  const goToPhase = useCallback((nextPhase) => {
    if (!PHASES[nextPhase]) {
      setError(`无效阶段: ${nextPhase}`);
      return;
    }
    setPhase(nextPhase);
    setError(null);
  }, []);

  const startDraw = useCallback(() => {
    setSessionId(`resam_${Date.now()}`);
    setIteration(0);
    setDetectResult(null);
    setTrainTaskId('');
    setTrainProgress(null);
    goToPhase(PHASES.DRAW);
  }, [goToPhase]);

  const startDetect = useCallback((result) => {
    setDetectResult(result);
    setIteration(prev => prev + 1);
    goToPhase(PHASES.SAM_DETECT);
  }, [goToPhase]);

  const startAnnotate = useCallback(() => {
    goToPhase(PHASES.ANNOTATE);
  }, [goToPhase]);

  const startRequery = useCallback((refinedResult) => {
    setDetectResult(refinedResult);
    setIteration(prev => prev + 1);
    goToPhase(PHASES.REQUERY);
  }, [goToPhase]);

  const canTrain = annotationCount >= ANNOTATION_THRESHOLD;

  /**
   * 提交 SSA 训练任务
   * @param {string[]} sessionIds - 标注 session ID 列表
   * @param {object} opts - { epochs, checkpointName }
   */
  const submitTrain = useCallback(async (sessionIds, opts = {}) => {
    if (!canTrain) {
      setError(`标注数量不足 (${annotationCount}/${ANNOTATION_THRESHOLD})，无法触发训练`);
      return null;
    }

    goToPhase(PHASES.TRAIN);
    try {
      const res = await fetch('/api/sam-train', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_ids: sessionIds,
          epochs: opts.epochs || 10,
          checkpoint_name: opts.checkpointName || `resam_v${iteration}`,
        }),
      });
      if (!res.ok) throw new Error((await res.text()).slice(0, 200));

      const data = await res.json();
      setTrainTaskId(data.task_id);

      // 轮询训练进度
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const sr = await fetch(`/api/sam-train/${data.task_id}/status`);
          if (!sr.ok) return;
          const status = await sr.json();
          setTrainProgress(status);

          if (status.status === 'complete') {
            clearInterval(pollRef.current);
            goToPhase(PHASES.COMPLETE);
          } else if (status.status === 'failed') {
            clearInterval(pollRef.current);
            setError(status.error || '训练失败');
          }
        } catch {}
      }, 3000);

      return data.task_id;
    } catch (err) {
      setError(err.message);
      return null;
    }
  }, [canTrain, iteration, goToPhase]);

  const reset = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    setPhase(PHASES.IDLE);
    setIteration(0);
    setSessionId('');
    setDetectResult(null);
    setAnnotationCount(0);
    setTrainTaskId('');
    setTrainProgress(null);
    setError(null);
  }, []);

  /**
   * 轮询获取标注数量（从后端 API）
   */
  const refreshAnnotationCount = useCallback(async (sid) => {
    try {
      const res = await fetch(`/api/annotations?session_id=${sid}`);
      if (!res.ok) return;
      const data = await res.json();
      setAnnotationCount(data.count || 0);
    } catch {}
  }, []);

  return {
    // 状态
    phase,
    phaseLabel: PHASE_LABELS[phase] || phase,
    phaseIndex,
    totalPhases: PHASE_ORDER.length - 1,  // 不含 IDLE
    iteration,
    sessionId,
    detectResult,
    annotationCount,
    annotationThreshold: ANNOTATION_THRESHOLD,
    canTrain,
    trainTaskId,
    trainProgress,
    error,

    // 操作
    startDraw,
    startDetect,
    startAnnotate,
    startRequery,
    submitTrain,
    reset,
    goToPhase,
    refreshAnnotationCount,
    setDetectResult,

    // 常量
    PHASES,
    PHASE_LABELS,
    PHASE_ORDER,
  };
}

export { PHASES, PHASE_LABELS, ANNOTATION_THRESHOLD };
