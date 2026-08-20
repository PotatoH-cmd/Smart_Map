import React from 'react';
import { PHASES, PHASE_LABELS } from '../hooks/useReSAMWorkflow';

const PHASE_DISPLAY_ORDER = [
  PHASES.DRAW,
  PHASES.SAM_DETECT,
  PHASES.ANNOTATE,
  PHASES.REQUERY,
  PHASES.TRAIN,
  PHASES.COMPLETE,
];

const STEP_ICONS = {
  [PHASES.DRAW]: '✏️',
  [PHASES.SAM_DETECT]: '🔍',
  [PHASES.ANNOTATE]: '🏷️',
  [PHASES.REQUERY]: '🔄',
  [PHASES.TRAIN]: '🧠',
  [PHASES.COMPLETE]: '✅',
};

const STEP_DESC = {
  [PHASES.DRAW]: '在地图上绘制识别区域',
  [PHASES.SAM_DETECT]: 'SAM3 目标自动检测',
  [PHASES.ANNOTATE]: '手动修正检测结果',
  [PHASES.REQUERY]: '对修正区域精修再审',
  [PHASES.TRAIN]: 'SSA 语义对齐微调',
  [PHASES.COMPLETE]: '训练完成，精度已提升',
};

/**
 * WorkflowStepper — ReSAM 工作流步骤可视化组件
 *
 * 纯展示组件，显示当前阶段、已完成阶段、进度百分比。
 * 支持点击手动切换至已完成阶段。
 *
 * Props:
 *   - currentPhase: 当前阶段 (PHASES 枚举值)
 *   - completedPhases: Set<string> 已完成的阶段名集合
 *   - onPhaseClick?: (phase) => void  点击阶段时回调
 *   - iteration?: 当前迭代轮次
 *   - annotationCount?: 标注数量
 */
const WorkflowStepper = ({
  currentPhase,
  completedPhases = new Set(),
  onPhaseClick,
  iteration = 0,
  annotationCount = 0,
}) => {
  const currentIdx = PHASE_DISPLAY_ORDER.indexOf(currentPhase);
  const totalSteps = PHASE_DISPLAY_ORDER.length;
  const progressPct = currentIdx >= 0
    ? Math.round(((currentIdx) / (totalSteps - 1)) * 100)
    : 0;

  return (
    <div style={{
      background: '#fff',
      borderRadius: 10,
      border: '1px solid #e2e8f0',
      padding: '14px 16px',
      marginBottom: 12,
      boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
    }}>
      {/* 标题行 */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10,
      }}>
        <div style={{ fontWeight: 700, fontSize: 13, color: '#1e293b' }}>
          🔄 ReSAM 工作流
          {iteration > 0 && (
            <span style={{ marginLeft: 8, fontSize: 11, color: '#64748b', fontWeight: 400 }}>
              第 {iteration} 轮
            </span>
          )}
        </div>
        {annotationCount > 0 && (
          <span style={{
            fontSize: 10, color: '#059669', background: '#ecfdf5',
            padding: '2px 8px', borderRadius: 10, fontWeight: 500,
          }}>
            {annotationCount} 条标注
          </span>
        )}
      </div>

      {/* 进度条 */}
      <div style={{
        height: 5, background: '#f1f5f9', borderRadius: 999, overflow: 'hidden', marginBottom: 12,
      }}>
        <div style={{
          width: `${Math.min(progressPct, 100)}%`,
          height: '100%',
          background: 'linear-gradient(90deg, #0ea5e9, #06b6d4, #7c3aed)',
          transition: 'width 0.4s ease',
          borderRadius: 999,
        }} />
      </div>

      {/* 步骤列表 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {PHASE_DISPLAY_ORDER.map((phase, idx) => {
          const isActive = phase === currentPhase;
          const isCompleted = completedPhases.has(phase) || (currentIdx >= 0 && idx < currentIdx);
          const isClickable = (isCompleted || idx <= currentIdx) && onPhaseClick;

          return (
            <div
              key={phase}
              onClick={() => isClickable && onPhaseClick(phase)}
              style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '7px 10px', borderRadius: 8,
                background: isActive ? '#f0f9ff' : 'transparent',
                border: isActive ? '1px solid #bae6fd' : '1px solid transparent',
                cursor: isClickable ? 'pointer' : 'default',
                opacity: isCompleted || isActive ? 1 : 0.45,
                transition: 'all 0.15s',
              }}
            >
              {/* 步骤图标/编号 */}
              <div style={{
                width: 28, height: 28, borderRadius: '50%',
                background: isCompleted
                  ? 'linear-gradient(135deg, #22c55e, #10b981)'
                  : isActive
                    ? 'linear-gradient(135deg, #0ea5e9, #06b6d4)'
                    : '#e2e8f0',
                color: isCompleted || isActive ? '#fff' : '#94a3b8',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 13, fontWeight: 700, flexShrink: 0,
                boxShadow: isActive ? '0 2px 6px rgba(14,165,233,0.3)' : 'none',
              }}>
                {isCompleted ? '✓' : STEP_ICONS[phase]}
              </div>

              {/* 步骤文字 */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  fontWeight: isActive ? 700 : 500,
                  fontSize: 12,
                  color: isActive ? '#0369a1' : isCompleted ? '#166534' : '#94a3b8',
                }}>
                  {idx + 1}. {PHASE_LABELS[phase]}
                  {isActive && (
                    <span style={{
                      marginLeft: 6, fontSize: 10, background: '#e0f2fe', color: '#0369a1',
                      padding: '1px 6px', borderRadius: 4, fontWeight: 600,
                    }}>
                      当前
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 10, color: '#94a3b8', marginTop: 1 }}>
                  {STEP_DESC[phase]}
                </div>
              </div>

              {/* 连接线箭头（非最后一个） */}
              {idx < PHASE_DISPLAY_ORDER.length - 1 && (
                <div style={{
                  position: 'absolute', left: 14, top: 36,
                  width: 1, height: 10,
                  background: isCompleted ? '#22c55e' : '#e2e8f0',
                }} />
              )}
            </div>
          );
        })}
      </div>

      {/* 底部提示 */}
      <div style={{
        marginTop: 10, paddingTop: 8,
        borderTop: '1px solid #f1f5f9',
        fontSize: 10, color: '#94a3b8', textAlign: 'center',
      }}>
        点击已完成步骤可跳转回顾
      </div>
    </div>
  );
};

export default WorkflowStepper;
