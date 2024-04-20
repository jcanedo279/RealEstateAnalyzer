document.addEventListener('DOMContentLoaded', function() {
    const form = document.getElementById('resetPasswordForm'); // Make sure the form has this ID

    form.addEventListener('submit', function(event) {
        event.preventDefault(); // Prevent the default form submission

        const newPassword = document.getElementById('new_password').value;
        const confirmPassword = document.getElementById('confirm_password').value;

        // Simple client-side validation for demonstration
        if (newPassword !== confirmPassword) {
            alert('Passwords do not match!');
            return;
        }

        // Assuming newPassword meets your complexity requirements
        submitNewPassword(newPassword);
    });

    function submitNewPassword(password) {
        const token = document.querySelector('input[name="token"]').value; // Ensure this input is included in the form

        fetch(`${form.action}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                new_password: password,
                token: token
            })
        }).then(response => response.json())
        .then(data => {
            if (data.success) {
                alert('Your password has been successfully updated.');
                window.location.href = '/login';
            } else {
                alert(data.message || 'Failed to reset password.');
            }
            if (data.redirect) {
                setTimeout(() => { window.location.href = data.redirect; }, 2000);
            } else {
                setTimeout(() => { window.location.href = '/login'; }, 2000);
            }
        }).catch(error => {
            console.error('Error:', error);
            alert('An error occurred while updating your password.');
        });
    }
});
