document.addEventListener('DOMContentLoaded', function() {
    const loginForm = document.getElementById('loginForm');
    
    loginForm.addEventListener('submit', function(event) {
        event.preventDefault();

        const formData = {
            user_email: document.getElementById('email').value,
            user_password: document.getElementById('password').value,
        };

        document.getElementById("loginBtn").disabled = true; // Disable button during request

        fetch('/login', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData),
        })
        .then(response => {
            document.getElementById("loginBtn").disabled = false;
            if (!response.ok) {
                throw new Error('Login failed');
            }
            return response.json();
        })
        .then(data => {
            if (data.redirect) {
                setTimeout(() => {
                    window.location.href = data.redirect;
                }, 2000);
            }
        })
        .catch((error) => {
            console.error('Error:', error);
            document.getElementById("loginBtn").disabled = false;
        });
    });
});
