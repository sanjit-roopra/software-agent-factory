/* Enhance the static stage explanations without making content depend on JavaScript. */
(() => {
  const selectFragment = () => {
    let fragment;
    try {
      fragment = decodeURIComponent(window.location.hash.slice(1));
    } catch {
      return;
    }
    const stage = document.getElementById(fragment)?.closest('.saf-stage');
    if (!stage) return;
    const button = document.getElementById(stage.getAttribute('aria-labelledby'));
    if (button) button.click();
  };
  const initialize = () => {
    document.querySelectorAll('[data-pipeline]').forEach((pipeline) => {
      if (pipeline.dataset.enhanced) return;
      const stages = [...pipeline.querySelectorAll('.saf-stage')];
      if (!stages.length) return;
      const tabs = document.createElement('div');
      tabs.className = 'saf-pipeline-tabs';
      tabs.setAttribute('role', 'tablist');
      tabs.setAttribute('aria-label', 'Pipeline stages');
      const buttons = stages.map((stage, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.id = `${stage.id}-tab`;
        button.setAttribute('role', 'tab');
        button.setAttribute('aria-controls', stage.id);
        const number = document.createElement('span');
        number.className = 'saf-tab-number';
        number.textContent = String(index + 1).padStart(2, '0');
        number.setAttribute('aria-hidden', 'true');
        button.append(number, document.createTextNode(stage.dataset.label));
        tabs.append(button);
        stage.setAttribute('role', 'tabpanel');
        stage.setAttribute('aria-labelledby', button.id);
        stage.tabIndex = 0;
        return button;
      });
      const select = (index, focus = false) => {
        stages.forEach((stage, position) => {
          const selected = position === index;
          stage.hidden = !selected;
          buttons[position].setAttribute('aria-selected', String(selected));
          buttons[position].tabIndex = selected ? 0 : -1;
        });
        if (focus) buttons[index].focus();
      };
      buttons.forEach((button, index) => {
        button.addEventListener('click', () => select(index));
        button.addEventListener('keydown', (event) => {
          let next;
          if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
          if (event.key === 'ArrowLeft') next = (index - 1 + buttons.length) % buttons.length;
          if (event.key === 'Home') next = 0;
          if (event.key === 'End') next = buttons.length - 1;
          if (next === undefined) return;
          event.preventDefault();
          select(next, true);
        });
      });
      pipeline.prepend(tabs);
      pipeline.dataset.enhanced = 'true';
      select(0);
    });
    selectFragment();
  };
  window.addEventListener('hashchange', selectFragment);
  // Material emits document$ after initial loading and each instant navigation.
  if (typeof document$ !== 'undefined') document$.subscribe(initialize);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
  else initialize();
})();
