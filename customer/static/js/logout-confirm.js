(function () {
    let overlay;
    let previousFocus;

    function closeDialog() {
        if (!overlay || overlay.hidden) return;
        overlay.hidden = true;
        overlay.setAttribute('aria-hidden', 'true');
        document.body.style.overflow = '';
        document.removeEventListener('keydown', handleKeydown);
        if (previousFocus instanceof HTMLElement) previousFocus.focus();
    }

    function confirmLogout() {
        const orders = sessionStorage.getItem('myOrders');
        sessionStorage.clear();
        if (orders) sessionStorage.setItem('myOrders', orders);
        window.location.href = '/customer/products/';
    }

    function handleKeydown(event) {
        if (event.key === 'Escape') {
            closeDialog();
            return;
        }

        if (event.key !== 'Tab' || !overlay || overlay.hidden) return;
        const buttons = overlay.querySelectorAll('button');
        const firstButton = buttons[0];
        const lastButton = buttons[buttons.length - 1];

        if (event.shiftKey && document.activeElement === firstButton) {
            event.preventDefault();
            lastButton.focus();
        } else if (!event.shiftKey && document.activeElement === lastButton) {
            event.preventDefault();
            firstButton.focus();
        }
    }

    window.showLogoutConfirmation = function () {
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.className = 'logout-confirm-overlay';
            overlay.hidden = true;
            overlay.setAttribute('aria-hidden', 'true');
            overlay.innerHTML = `
                <div class="logout-confirm-dialog" role="dialog" aria-modal="true"
                    aria-labelledby="logoutConfirmTitle" aria-describedby="logoutConfirmDescription">
                    <div class="logout-confirm-icon" aria-hidden="true">↪</div>
                    <h2 id="logoutConfirmTitle">Log out of your account?</h2>
                    <p id="logoutConfirmDescription">You can sign in again anytime to continue shopping and view your orders.</p>
                    <div class="logout-confirm-actions">
                        <button class="logout-confirm-cancel" type="button">Cancel</button>
                        <button class="logout-confirm-submit" type="button">Log Out</button>
                    </div>
                </div>
            `;
            overlay.addEventListener('click', function (event) {
                if (event.target === overlay) closeDialog();
            });
            overlay.querySelector('.logout-confirm-cancel').addEventListener('click', closeDialog);
            overlay.querySelector('.logout-confirm-submit').addEventListener('click', confirmLogout);
            document.body.appendChild(overlay);
        }

        previousFocus = document.activeElement;
        overlay.hidden = false;
        overlay.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
        document.addEventListener('keydown', handleKeydown);
        overlay.querySelector('.logout-confirm-cancel').focus();
    };
})();
