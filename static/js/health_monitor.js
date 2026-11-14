/**
 * Global System Health, Operating Mode & Emergency Stop Monitor
 *
 * Continuously polls /api/v2/system/mode and /api/v2/risk/metrics to synchronize
 * the sticky top banner, monitor heartbeat latency, and handle emergency shutdown.
 */

(function () {
  'use strict';

  let pollInterval = null;
  let isRequestInFlight = false;

  function fmtMoney(val) {
    if (val === null || val === undefined || isNaN(val)) return '$0.00';
    const num = parseFloat(val);
    const sign = num > 0 ? '+' : num < 0 ? '-' : '';
    return `${sign}$${Math.abs(num).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function injectGlobalBannerIfMissing() {
    if (document.getElementById('globalBanner')) return;

    const path = window.location.pathname;
    const isTerminal = path.startsWith('/terminal');
    const isAnomalies = path === '/' || path.startsWith('/index');
    const isBot = path.startsWith('/bot');
    const isStrategies = path.startsWith('/strategies');

    const bannerHtml = `
      <header id="globalBanner" style="position:sticky;top:0;z-index:10000;background:rgba(17,20,25,0.95);backdrop-filter:blur(8px);border-bottom:1px solid #242933;padding:8px 18px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;color:#e6edf3;font-family:'Inter',sans-serif;font-size:12px;">
        <div style="display:flex;align-items:center;gap:14px;">
          <div style="font-size:13px;font-weight:700;letter-spacing:0.5px;display:flex;align-items:center;gap:8px;">
            <span style="color:#f0b90b;">⚡</span> DELTA INDIA TERMINAL
          </div>
          <div id="bannerMode" class="mode-badge mode-paper" style="padding:3px 10px;border-radius:100px;font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;letter-spacing:0.5px;background:rgba(5,199,147,0.15);color:#05c793;border:1px solid rgba(5,199,147,0.4);">
            MODE: PAPER
          </div>
          <div style="display:flex;align-items:center;gap:6px;font-family:'JetBrains Mono',monospace;font-size:11px;color:#7d8590;">
            <span id="bannerDot" class="status-dot" style="width:7px;height:7px;border-radius:50%;background:#05c793;box-shadow:0 0 6px #05c793;"></span>
            <span id="bannerConnStatus">CONNECTED</span>
            <span id="bannerLatency" style="color:#7d8590;">(12ms)</span>
          </div>
        </div>

        <div style="display:flex;gap:4px;background:#161b22;padding:3px;border-radius:8px;border:1px solid #242933;">
          <a href="/terminal" style="padding:5px 12px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:500;color:${isTerminal ? '#111419' : '#7d8590'};background:${isTerminal ? '#f0b90b' : 'transparent'};">Ladder Terminal</a>
          <a href="/" style="padding:5px 12px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:500;color:${isAnomalies ? '#111419' : '#7d8590'};background:${isAnomalies ? '#f0b90b' : 'transparent'};">Anomaly Board</a>
          <a href="/bot" style="padding:5px 12px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:500;color:${isBot ? '#111419' : '#7d8590'};background:${isBot ? '#f0b90b' : 'transparent'};">Risk & Bot</a>
          <a href="/strategies" style="padding:5px 12px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:500;color:${isStrategies ? '#111419' : '#7d8590'};background:${isStrategies ? '#f0b90b' : 'transparent'};">Strategy Manager</a>
        </div>

        <div style="display:flex;align-items:center;gap:14px;">
          <div style="display:flex;align-items:center;gap:6px;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:#7d8590;">
            <span>Day PnL:</span>
            <b id="bannerDayPnl" style="color:#05c793;">+$0.00</b>
            <span style="opacity:0.6;">/</span>
            <span id="bannerLossLimit">-$50.00 Max</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:#7d8590;">
            <span>Margin:</span>
            <b id="bannerMargin" style="color:#e6edf3;">0.0%</b>
          </div>
          <button id="bannerEmergencyStop" onclick="openEmergencyModal()" style="background:#ff2a3b;color:#fff;border:1px solid #ff4757;padding:6px 14px;border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;cursor:pointer;">
            🚨 EMERGENCY STOP
          </button>
        </div>
      </header>

      <div id="emergencyModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);z-index:99999;align-items:center;justify-content:center;">
        <div style="background:#111419;border:1px solid #ff4757;border-radius:12px;padding:24px;max-width:440px;width:90%;color:#e6edf3;">
          <h3 style="color:#ff4757;font-size:16px;margin-bottom:12px;">⚠️ ENGAGE EMERGENCY STOP</h3>
          <p style="color:#7d8590;line-height:1.5;margin-bottom:20px;font-size:13px;">
            This will immediately halt all automated scanning and algorithmic strategies,
            trip the emergency circuit breaker, and dispatch a <b>CANCEL ALL</b> command
            to purge resting exchange and paper orders.
          </p>
          <div style="display:flex;justify-content:flex-end;gap:12px;">
            <button onclick="closeEmergencyModal()" style="padding:8px 16px;background:#161b22;border:1px solid #242933;color:#e6edf3;border-radius:6px;cursor:pointer;">Cancel</button>
            <button onclick="confirmEmergencyStop()" style="padding:8px 18px;background:#ff4757;border:1px solid #ff4757;color:#fff;border-radius:6px;font-weight:700;cursor:pointer;">Confirm Emergency Halt</button>
          </div>
        </div>
      </div>
    `;

    const wrapper = document.createElement('div');
    wrapper.innerHTML = bannerHtml;
    while (wrapper.firstChild) {
      document.body.prepend(wrapper.lastChild);
    }
  }

  function updateModeBadge(mode, isLive) {
    const el = document.getElementById('bannerMode');
    if (!el) return;

    el.className = 'mode-badge';
    const norm = (mode || 'PAPER').toUpperCase();

    if (norm.includes('PAPER')) {
      el.classList.add('mode-paper');
      el.textContent = 'MODE: PAPER TRADING';
    } else if (norm.includes('TESTNET')) {
      el.classList.add('mode-testnet');
      el.textContent = 'MODE: TESTNET';
    } else if (norm.includes('LIVE')) {
      el.classList.add('mode-live');
      el.textContent = norm.includes('AUTO') ? '⚠️ LIVE AUTOMATED' : 'MODE: LIVE MANUAL';
    } else if (norm.includes('BACKTEST')) {
      el.classList.add('mode-backtest');
      el.textContent = 'MODE: BACKTEST';
    } else {
      el.classList.add('mode-paper');
      el.textContent = `MODE: ${norm}`;
    }
  }

  function updateConnectionStatus(connected, latencyMs, circuitBreakerTripped) {
    const dot = document.getElementById('bannerDot');
    const statusText = document.getElementById('bannerConnStatus');
    const latencyText = document.getElementById('bannerLatency');

    if (!dot || !statusText) return;

    if (!connected) {
      dot.className = 'status-dot disconnected';
      statusText.textContent = 'DISCONNECTED';
      if (latencyText) latencyText.textContent = '(timeout)';
    } else if (circuitBreakerTripped) {
      dot.className = 'status-dot warning';
      statusText.textContent = 'CIRCUIT BREAKER';
      if (latencyText) latencyText.textContent = `(${latencyMs}ms)`;
    } else {
      dot.className = 'status-dot';
      statusText.textContent = 'CONNECTED';
      if (latencyText) latencyText.textContent = `(${latencyMs}ms)`;
    }
  }

  function updateMetrics(riskData) {
    if (!riskData) return;

    const pnlEl = document.getElementById('bannerDayPnl');
    const lossLimitEl = document.getElementById('bannerLossLimit');
    const marginEl = document.getElementById('bannerMargin');

    if (pnlEl) {
      const pnl = parseFloat(riskData.daily_pnl || 0);
      pnlEl.textContent = fmtMoney(pnl);
      pnlEl.style.color = pnl >= 0 ? 'var(--green, #05c793)' : 'var(--red, #ff4757)';
    }

    if (lossLimitEl && riskData.daily_loss_limit !== undefined) {
      lossLimitEl.textContent = `-$${parseFloat(riskData.daily_loss_limit).toFixed(2)} Max`;
    }

    if (marginEl) {
      const marginPct = parseFloat(riskData.margin_utilization_pct || 0);
      marginEl.textContent = `${marginPct.toFixed(1)}%`;
      if (marginPct > 80) {
        marginEl.style.color = '#ff4757';
      } else if (marginPct > 50) {
        marginEl.style.color = '#f0b90b';
      } else {
        marginEl.style.color = '#e6edf3';
      }
    }
  }

  async function pollHealthAndMode() {
    if (isRequestInFlight) return;
    isRequestInFlight = true;

    const startTime = performance.now();
    try {
      const [modeRes, riskRes] = await Promise.all([
        fetch('/api/v2/system/mode', { credentials: 'include' }).catch(() => null),
        fetch('/api/v2/risk/metrics', { credentials: 'include' }).catch(() => null),
      ]);

      const latencyMs = Math.round(performance.now() - startTime);

      if (modeRes && modeRes.ok) {
        const modeData = await modeRes.json();
        updateModeBadge(modeData.environment, modeData.is_live);
        updateConnectionStatus(true, latencyMs, modeData.circuit_breaker_active);
      } else {
        updateConnectionStatus(false, latencyMs, false);
      }

      if (riskRes && riskRes.ok) {
        const riskData = await riskRes.json();
        updateMetrics(riskData);
      }
    } catch (e) {
      updateConnectionStatus(false, 0, false);
    } finally {
      isRequestInFlight = false;
    }
  }

  // ── Emergency Stop Modal Handlers ──
  window.openEmergencyModal = function () {
    const modal = document.getElementById('emergencyModal');
    if (modal) modal.style.display = 'flex';
  };

  window.closeEmergencyModal = function () {
    const modal = document.getElementById('emergencyModal');
    if (modal) modal.style.display = 'none';
  };

  window.confirmEmergencyStop = async function () {
    const modal = document.getElementById('emergencyModal');
    const btn = document.getElementById('bannerEmergencyStop');
    if (btn) btn.disabled = true;

    try {
      const res = await fetch('/api/v2/system/emergency-stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
      });
      const data = await res.json();
      alert(data.message || 'EMERGENCY STOP ENGAGED: Circuit breaker tripped and active orders cancelled.');
    } catch (e) {
      alert('Error triggering emergency stop: ' + e);
    } finally {
      if (modal) modal.style.display = 'none';
      if (btn) btn.disabled = false;
      pollHealthAndMode();
    }
  };

  // Start polling
  document.addEventListener('DOMContentLoaded', () => {
    injectGlobalBannerIfMissing();
    pollHealthAndMode();
    pollInterval = setInterval(pollHealthAndMode, 2500);
  });
})();
