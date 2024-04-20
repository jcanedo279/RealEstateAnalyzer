document.addEventListener('DOMContentLoaded', function() {
    const resetForm = document.getElementById('resetForm');
    const deducedEmailInput = document.getElementById('deducedEmail'); // Hidden input for authenticated users

    if (deducedEmailInput && deducedEmailInput.value) {
        // Automatically submit the form if the user is logged in
        resetForm.submit();
    }

    resetForm.addEventListener('submit', function(event) {
        event.preventDefault();
        const emailInput = document.getElementById('email');
        const formData = { email: emailInput ? emailInput.value : deducedEmailInput.value };

        fetch('/reset-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        })
        .then(response => response.json())
        .then(data => {
            if (data.redirect) {
                setTimeout(() => { window.location.href = data.redirect; }, 2000);
            }
        })
        .catch(error => {
            console.error('Error:', error);
        });
    });
});
