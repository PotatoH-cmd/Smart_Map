// utils/chart.js — ECharts 图表格式化工具（从 App.jsx 抽离）

export const CHART_FONT_FAMILY = "'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', 'Source Han Sans SC', 'SimHei', Arial, sans-serif";

export function formatChartNumber(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return value;
  const abs = Math.abs(value);
  if (abs >= 10000) return `${(value / 10000).toFixed(2)}万`;
  if (abs >= 1000) return value.toLocaleString('zh-CN', { maximumFractionDigits: 1 });
  if (abs >= 1) return value.toFixed(2).replace(/\.00$/, '');
  return value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

export function wrapChartText(text, maxLength) {
  if (!text || typeof text !== 'string') return text;
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength || normalized.includes('\n')) return normalized;
  const chunks = [];
  for (let i = 0; i < normalized.length; i += maxLength) {
    chunks.push(normalized.slice(i, i + maxLength));
  }
  return chunks.join('\n');
}

export function flattenChartText(text) {
  if (!text || typeof text !== 'string') return '';
  return text.replace(/\s*\n\s*/g, '').trim();
}

export function normalizeChartOption(rawOption, chartType = 'bar') {
  if (!rawOption || typeof rawOption !== 'object') return rawOption;

  let option;
  try {
    option = JSON.parse(JSON.stringify(rawOption));
  } catch {
    option = { ...rawOption };
  }

  option.backgroundColor = 'transparent';
  option.animationDuration = option.animationDuration ?? 600;
  option.animationEasing = option.animationEasing || 'cubicOut';
  option.textStyle = {
    color: '#334155',
    fontFamily: CHART_FONT_FAMILY,
    ...(option.textStyle || {}),
  };

  option.tooltip = {
    trigger: option.tooltip?.trigger || (chartType === 'pie' ? 'item' : 'axis'),
    backgroundColor: 'rgba(15, 23, 42, 0.92)',
    borderWidth: 0,
    padding: [10, 12],
    textStyle: {
      color: '#e2e8f0',
      fontSize: 12,
      lineHeight: 18,
      ...(option.tooltip?.textStyle || {}),
    },
    axisPointer: {
      type: 'shadow',
      shadowStyle: { color: 'rgba(59, 130, 246, 0.12)' },
      ...(option.tooltip?.axisPointer || {}),
    },
    ...option.tooltip,
  };

  if (option.title) {
    const rawTitle = option.title;
    const titleText = wrapChartText(rawTitle.text, 18);
    const subtext = wrapChartText(rawTitle.subtext, 30);
    option.title = {
      left: 'center',
      top: 8,
      itemGap: 8,
      textStyle: {
        color: '#0f172a',
        fontFamily: CHART_FONT_FAMILY,
        fontSize: 14,
        fontWeight: 700,
        lineHeight: 20,
        width: 300,
        overflow: 'break',
        ...(option.title.textStyle || {}),
      },
      subtextStyle: {
        color: '#64748b',
        fontFamily: CHART_FONT_FAMILY,
        fontSize: 12,
        lineHeight: 18,
        width: 320,
        overflow: 'break',
        ...(option.title.subtextStyle || {}),
      },
      ...option.title,
      text: titleText,
      subtext,
    };
  }

  if (option.legend) {
    option.legend = {
      top: 48,
      left: 'center',
      icon: 'roundRect',
      itemWidth: 12,
      itemHeight: 8,
      textStyle: {
        color: '#475569',
        fontFamily: CHART_FONT_FAMILY,
        fontSize: 12,
        ...(option.legend.textStyle || {}),
      },
      ...option.legend,
    };
  }

  if (chartType !== 'pie' && chartType !== 'card') {
    option.grid = {
      left: 56,
      right: 24,
      top: 28,
      bottom: 62,
      containLabel: true,
      ...(option.grid || {}),
    };

    const normalizeAxis = (axis) => {
      if (!axis) return axis;
      const next = {
        axisLine: { show: false, ...(axis.axisLine || {}) },
        axisTick: { show: false, ...(axis.axisTick || {}) },
        axisLabel: {
          color: '#64748b',
          fontSize: 12,
          hideOverlap: true,
          margin: 12,
          ...(axis.axisLabel || {}),
        },
        splitLine: {
          show: axis.type === 'value' || axis.type === 'log',
          lineStyle: {
            color: '#e2e8f0',
            width: 1,
            type: 'solid',
            ...(axis.splitLine?.lineStyle || {}),
          },
          ...(axis.splitLine || {}),
        },
        ...axis,
      };

      if (next.type === 'value' || next.type === 'log') {
        next.axisLabel = {
          ...next.axisLabel,
          formatter: next.axisLabel?.formatter || ((v) => formatChartNumber(v)),
        };
      }
      return next;
    };

    if (Array.isArray(option.xAxis)) option.xAxis = option.xAxis.map(normalizeAxis);
    else option.xAxis = normalizeAxis(option.xAxis);

    if (Array.isArray(option.yAxis)) option.yAxis = option.yAxis.map(normalizeAxis);
    else option.yAxis = normalizeAxis(option.yAxis);
  }

  if (Array.isArray(option.series)) {
    option.series = option.series.map((series) => {
      if (!series || typeof series !== 'object') return series;
      const seriesValues = Array.isArray(series.data)
        ? series.data
            .map((item) => {
              if (typeof item === 'number') return item;
              if (Array.isArray(item)) return Number(item[item.length - 1]);
              if (item && typeof item === 'object') {
                if (typeof item.value === 'number') return item.value;
                if (Array.isArray(item.value)) return Number(item.value[item.value.length - 1]);
              }
              return NaN;
            })
            .filter((v) => typeof v === 'number' && !Number.isNaN(v))
        : [];
      const nextSeries = {
        ...series,
        emphasis: {
          focus: 'series',
          ...(series.emphasis || {}),
        },
      };

      if (nextSeries.type === 'bar') {
        nextSeries.barMaxWidth = series.barMaxWidth || 42;
        nextSeries.clip = false;

        const titleText = `${option?.title?.text || ''} ${series.name || ''}`;
        const categories = Array.isArray(option?.xAxis?.data)
          ? option.xAxis.data.map((c) => String(c || ''))
          : [];
        const avgIdx = categories.findIndex((c) => c.includes('平均'));
        const hasTripletMetrics = categories.some((c) => c.includes('最大')) && categories.some((c) => c.includes('最小'));
        const isDepthThresholdChart = /超深|阈值|判定/.test(titleText) && (seriesValues.length === 1 || hasTripletMetrics || avgIdx >= 0);
        const threshold = 2;
        const currentValue = avgIdx >= 0 ? seriesValues[avgIdx] : seriesValues[0];
        const isExceeded = isDepthThresholdChart && typeof currentValue === 'number' && currentValue > threshold;

        nextSeries.itemStyle = {
          borderRadius: [8, 8, 0, 0],
          color: (params) => {
            if (isDepthThresholdChart && typeof params?.value === 'number') {
              const name = String(params?.value || '');
              if (name.includes('平均')) {
                return params.value > threshold ? '#ef4444' : '#22c55e';
              }
              return '#3b82f6';
            }
            const v = params?.value;
            if (typeof v === 'number' && v < 0) return '#f97316';
            return '#3b82f6';
          },
          ...(series.itemStyle || {}),
        };
        nextSeries.label = {
          show: true,
          position: (params) => {
            const v = Array.isArray(params?.value) ? params.value[1] : params?.value;
            return typeof v === 'number' && v < 0 ? 'bottom' : 'top';
          },
          distance: 8,
          color: '#1e293b',
          fontSize: 12,
          formatter: ({ value }) => formatChartNumber(value),
          hideOverlap: true,
          ...(series.label || {}),
        };
        nextSeries.labelLayout = {
          hideOverlap: true,
          moveOverlap: 'shiftY',
          ...(series.labelLayout || {}),
        };

        if (isDepthThresholdChart) {
          nextSeries.markLine = {
            symbol: 'none',
            silent: true,
            label: {
              show: true,
              formatter: `阈值 ${threshold}m`,
              color: '#64748b',
              fontSize: 11,
              backgroundColor: '#f8fafc',
              padding: [2, 6],
              borderRadius: 4,
            },
            lineStyle: {
              color: '#94a3b8',
              type: 'dashed',
              width: 1.5,
            },
            data: [{ yAxis: threshold }],
            ...(series.markLine || {}),
          };

          if (option.title) {
            option.title = {
              ...(option.title || {}),
              subtext: wrapChartText(`判定依据：平均差值 ${formatChartNumber(currentValue)} m ｜ 阈值 ${threshold} m ｜ ${isExceeded ? '超标' : '未超标'}（最大/最小仅作参考）`, 30),
              subtextStyle: {
                color: isExceeded ? '#dc2626' : '#16a34a',
                fontFamily: CHART_FONT_FAMILY,
                fontSize: 12,
                lineHeight: 18,
                fontWeight: 600,
                width: 320,
                overflow: 'break',
                ...((option.title && option.title.subtextStyle) || {}),
              },
            };
          }
        }
      } else if (nextSeries.type === 'line') {
        nextSeries.symbol = series.symbol || 'circle';
        nextSeries.symbolSize = series.symbolSize || 8;
        nextSeries.smooth = series.smooth ?? true;
        nextSeries.clip = false;
        nextSeries.lineStyle = {
          width: 3,
          ...(series.lineStyle || {}),
        };
        nextSeries.labelLayout = {
          hideOverlap: true,
          moveOverlap: 'shiftY',
          ...(series.labelLayout || {}),
        };
      } else if (nextSeries.type === 'pie') {
        nextSeries.radius = series.radius || ['38%', '66%'];
        nextSeries.center = series.center || ['50%', '58%'];
        nextSeries.label = {
          color: '#334155',
          fontSize: 12,
          formatter: series.label?.formatter || '{b}\n{d}%',
          ...(series.label || {}),
        };
      }
      return nextSeries;
    });
  }

  if (option.title) {
    const headerTitle = flattenChartText(option.title.text);
    const headerSubtext = flattenChartText(option.title.subtext);
    option.__chartHeader = {
      title: headerTitle,
      subtext: headerSubtext,
      subtextColor: option.title.subtextStyle?.color,
    };
    option.title = {
      ...option.title,
      show: false,
      text: '',
      subtext: '',
    };
  }

  if (option.legend) {
    const seriesCount = Array.isArray(option.series) ? option.series.length : 0;
    option.legend = {
      ...option.legend,
      show: seriesCount > 1 && option.legend.show !== false,
      bottom: 4,
      left: 'center',
    };
    delete option.legend.top;
  }

  return option;
}

export function getChartMessageKey(message) {
  if (!message) return null;
  try {
    const raw = typeof message.content === 'string' ? JSON.parse(message.content) : message.content;
    if (!raw?.config) return null;
    const title = raw.config?.title?.text || raw.content || raw.formatted_answer || '';
    return `${raw.chart_type || 'chart'}_${title}_${JSON.stringify(raw.config)}`;
  } catch {
    return null;
  }
}

export function buildChartMessages(charts = []) {
  if (!Array.isArray(charts)) return [];

  return charts
    .filter(chart => chart && chart.config)
    .map((chart, index) => ({
      role: 'function',
      name: 'data_visualizer_tool',
      content: JSON.stringify({
        success: true,
        chart_type: chart.chart_type || 'bar',
        config: chart.config,
        content: chart.summary || '',
        __chart_id: `${chart.chart_type || 'chart'}_${chart.config?.title?.text || index}`,
      }),
    }));
}

export function getMessageKey(message) {
  const chartKey = getChartMessageKey(message);
  if (chartKey) return `chart_${chartKey}`;
  // 携带唯一 mid 的消息不参与内容去重（同一句回复在不同轮次合法重复）
  if (message?.mid) return `${message.role || ''}_${message.mid}`;
  return `${message?.role || ''}_${message?.name || ''}_${message?.content || ''}`;
}

export function generateSessionId() {
  const cryptoObj = typeof window !== 'undefined' ? window.crypto : undefined;

  if (cryptoObj?.randomUUID) {
    return cryptoObj.randomUUID();
  }

  // 降级方案：基于时间+随机数生成
  return Date.now().toString(36) + Math.random().toString(36).substr(2);
}

export function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 86400000 && now.getDate() === d.getDate()) return '今天';
  if (diff < 172800000) return '昨天';
  return `${d.getMonth() + 1}/${d.getDate()}`;
}
