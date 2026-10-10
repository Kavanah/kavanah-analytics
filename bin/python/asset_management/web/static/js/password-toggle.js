/* Wires up <button class="pw-toggle" data-target="<input-id>"> to flip
   its target input between type=password and type=text. Idempotent —
   safe to load on pages without any toggle buttons. */
(function () {
    function wire() {
        document.querySelectorAll('.pw-toggle').forEach(function (btn) {
            if (btn.dataset.pwToggleWired === '1') return;
            btn.dataset.pwToggleWired = '1';
            btn.addEventListener('click', function () {
                var input = document.getElementById(btn.getAttribute('data-target'));
                if (!input) return;
                var revealed = input.getAttribute('type') === 'text';
                input.setAttribute('type', revealed ? 'password' : 'text');
                btn.classList.toggle('is-visible', !revealed);
                btn.setAttribute('aria-pressed', String(!revealed));
                btn.setAttribute('aria-label', revealed ? 'Show password' : 'Hide password');
            });
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }
})();
