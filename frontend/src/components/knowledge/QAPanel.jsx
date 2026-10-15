// QAPanel.jsx — 智能问答 tab（从 KnowledgeBaseManager.jsx 拆分）
import { useRef, useEffect } from 'react';
import { styles } from './styles';
import { ThinkingPanel, SourcePanel } from './common';

const QAPanel = ({ qaMessages, qaInput, setQaInput, qaLoading, onSend }) => {
  const qaEndRef = useRef(null);

  useEffect(() => {
    if (qaEndRef.current) {
      qaEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [qaMessages]);

  const handleSend = () => {
    if (qaInput.trim() && !qaLoading) {
      onSend(qaInput.trim());
    }
  };

  return (
    <div style={styles.qaContainer}>
      <div style={styles.qaMessages}>
        {qaMessages.length === 0 && (
          <div style={styles.qaEmpty}>
            <span style={{ fontSize: 32 }}>?</span>
            <span>输入问题，从知识库中智能检索并生成回答</span>
          </div>
        )}
        {qaMessages.map((msg) => (
          <div key={msg.id} style={styles.qaMsg(msg.role)}>
            <div style={styles.qaAvatar(msg.role)}>
              {msg.role === 'user' ? '我' : 'AI'}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={styles.qaBubble(msg.role)}>
                {msg.content}
              </div>
              {msg.thinkingSteps && msg.thinkingSteps.length > 0 && (
                <ThinkingPanel steps={msg.thinkingSteps} />
              )}
              {msg.sources && msg.sources.length > 0 && (
                <SourcePanel sources={msg.sources} />
              )}
            </div>
          </div>
        ))}
        {qaLoading && (
          <div style={styles.qaMsg('assistant')}>
            <div style={styles.qaAvatar('assistant')}>AI</div>
            <div style={styles.qaBubble('assistant')}>
              <div style={styles.qaLoadingDots}>
                <div style={styles.dot(0)} />
                <div style={styles.dot(1)} />
                <div style={styles.dot(2)} />
              </div>
            </div>
          </div>
        )}
        <div ref={qaEndRef} />
      </div>
      <div style={styles.qaInputArea}>
        <input
          style={styles.qaInput}
          type="text"
          placeholder="输入问题，搜索知识库..."
          value={qaInput}
          onChange={e => setQaInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSend()}
        />
        <button
          style={styles.qaSendBtn(qaInput.trim() && !qaLoading)}
          onClick={handleSend}
          disabled={!qaInput.trim() || qaLoading}
        >
          {qaLoading ? '思考中' : '发送'}
        </button>
      </div>
    </div>
  );
};

export default QAPanel;