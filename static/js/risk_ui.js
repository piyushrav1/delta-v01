/**
 * Risk & Circuit Breaker Status UI Monitor
 *
 * Renders real-time visual meters for margin utilization, drawdown against daily loss cap,
 * per-strategy risk exposure, and active circuit breaker states.
 */

(function () {
  'use strict';

  function fmtUSD(val) {
    if (val === null || val === undefined) return '$0.00';
    const n = parseFloat(val);
    const sign = n < 0 ? '-' : '';
    return `${sign}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function renderMarginMeter(marginUsed, availableBal, totalEquity, marginPct) {
    const barEl = document.getElementById('marginProgressBar');
    const pctEl = document.getElementById('marginPctLabel');
    const usedEl = document.getElementById('marginUsedVal');
    const availEl = document.getElementById('marginAvailVal');
    const equityEl = document.getElementById('marginEquityVal');

    const pct = Math.min(100, Math.max(0, parseFloat(marginPct || 0)));

    if (barEl) {
      barEl.style.width = `${pct}%`;
      if (pct > 80) {
        barEl.style.backgroundColor = 'var(--red)';
      } else if (pct > 50) {
        barEl.style.backgroundColor = 'var(--amber)';
      } else {
        barEl.style.backgroundColor = 'var(--green)';
      }
    }

    if (pctEl) pctEl.textContent = `${pct.toFixed(1)}%`;
    if (usedEl) usedEl.textContent = fmtUSD(marginUsed);
    if (availEl) availEl.textContent = fmtUSD(availableBal);
    if (equityEl) equityEl.textContent = fmtUSD(totalEquity);
  }

  function renderDrawdownMeter(dailyPnl, lossLimit, peakDrawdownPct) {
    const pnlValEl = document.getElementById('riskDailyPnlVal');
    const lossLimitValEl = document.getElementById('riskLossLimitVal');
    const lossRemainingValEl = document.getElementById('riskLossRemainingVal');
    const ddBarEl = document.getElementById('drawdownProgressBar');
    const ddPctEl = document.getElementById('drawdownPctLabel');

    const pnl = parseFloat(dailyPnl || 0);
    const limit = parseFloat(lossLimit || 50.0);
    const remaining = Math.max(0, limit + pnl);

    if (pnlValEl) {
      pnlValEl.textContent = fmtUSD(pnl);
      pnlValEl.className = pnl >= 0 ? 'profit' : 'loss';
    }
    if (lossLimitValEl) lossLimitValEl.textContent = `-${fmtUSD(limit)}`;
    if (lossRemainingValEl) lossRemainingValEl.textContent = fmtUSD(remaining);

    // Drawdown utilization of daily loss budget
    const budgetUsedPct = pnl < 0 ? Math.min(100, (Math.abs(pnl) / limit) * 100) : 0;
    if (ddBarEl) {
      ddBarEl.style.width = `${budgetUsedPct}%`;
      if (budgetUsedPct > 80) ddBarEl.style.backgroundColor = 'var(--red)';
      else if (budgetUsedPct > 50) ddBarEl.style.backgroundColor = 'var(--amber)';
      else ddBarEl.style.backgroundColor = 'var(--green)';
    }
    if (ddPctEl) ddPctEl.textContent = `${budgetUsedPct.toFixed(1)}%`;
  }

  function renderStrategyExposure(exposures) {
    const container = document.getElementById('strategyExposureContainer');
    if (!container || !exposures) return;

    const entries = Object.entries(exposures);
    if (!entries.length) {
      container.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:12px 0;">No active strategy exposure</div>';
      return;
    }

    const totalExp = entries.reduce((acc, [_, v]) => acc + parseFloat(v || 0), 0) || 1.0;

    container.innerHTML = entries.map(([stratId, exp]) => {
      const amt = parseFloat(exp || 0);
      const sharePct = Math.min(100, Math.round((amt / totalExp) * 100));
      const label = stratId.replace(/_/g, ' ').toUpperCase();

      return `
        <div style="margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;font-size:11.5px;margin-bottom:4px;">
            <span style="font-weight:600;color:var(--text);">${label}</span>
            <span class="mono" style="color:var(--amber);">${fmtUSD(amt)} (${sharePct}%)</span>
          </div>
          <div style="height:6px;background:var(--panel-3);border-radius:3px;overflow:hidden;">
            <div style="height:100%;width:${sharePct}%;background:var(--blue);border-radius:3px;"></div>
          </div>
        </div>
      `;
    }).join('');
  }

  function renderCircuitBreakers(activeBreakers) {
    const container = document.getElementById('circuitBreakerContainer');
    if (!container) return;

    const breakers = [
      { id: 'daily_loss', name: 'Daily Loss Cap Breaker', desc: 'Triggers on cumulative loss ≥ configured cap' },
      { id: 'exchange_timeout', name: 'Heartbeat Timeout Breaker', desc: 'Triggers if exchange data gap > 10s' },
      { id: 'order_rejections', name: 'Order Rejections Breaker', desc: 'Triggers on ≥3 consecutive exchange rejections' },
    ];

    const activeIds = (activeBreakers || []).map(b => (typeof b === 'string' ? b : b.breaker_type || b.id));

    container.innerHTML = breakers.map(b => {
      const isTripped = activeIds.includes(b.id);
      return `
        <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--panel-2);border:1px solid ${isTripped ? 'var(--red)' : 'var(--line)'};border-radius:8px;margin-bottom:8px;">
          <div>
            <div style="font-weight:600;font-size:12px;color:${isTripped ? 'var(--red)' : 'var(--text)'};">${b.name}</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">${b.desc}</div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;">
            <span class="mono" style="padding:3px 8px;border-radius:4px;font-size:10.5px;font-weight:700;${isTripped ? 'background:rgba(255,71,87,0.2);color:var(--red);border:1px solid var(--red);' : 'background:rgba(5,199,147,0.15);color:var(--green);border:1px solid rgba(5,199,147,0.3);'}">
              ${isTripped ? '⚠️ TRIPPED' : 'NORMAL'}
            </span>
            ${isTripped ? `<button onclick="resetCircuitBreaker('${b.id}')" style="padding:4px 10px;background:var(--red);color:#fff;border:none;border-radius:4px;font-size:10.5px;font-weight:600;cursor:pointer;">Reset</button>` : ''}
          </div>
        </div>
      `;
    }).join('');
  }

  window.resetCircuitBreaker = async function (breakerId) {
    try {
      const res = await fetch('/api/v2/circuit-breaker/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ breaker_id: breakerId }),
        credentials: 'include',
      });
      const data = await res.json();
      alert(data.message || 'Circuit breaker reset successfully.');
      refreshRiskData();
    } catch (e) {
      alert('Error resetting breaker: ' + e);
    }
  };

  async function refreshRiskData() {
    try {
      const res = await fetch('/api/v2/risk/metrics', { credentials: 'include' });
      if (!res.ok) return;
      const data = await res.json();

      renderMarginMeter(data.margin_used, data.available_balance, data.total_equity, data.margin_utilization_pct);
      renderDrawdownMeter(data.daily_pnl, data.daily_loss_limit, data.peak_drawdown_pct);
      renderStrategyExposure(data.strategy_exposure);
      renderCircuitBreakers(data.active_circuit_breakers);
    } catch (e) {
      console.warn('Error fetching risk metrics:', e);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    refreshRiskData();
    setInterval(refreshRiskData, 3000);
  });
})();
