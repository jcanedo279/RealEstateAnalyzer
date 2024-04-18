document.addEventListener('DOMContentLoaded', function() {
    const registerForm = document.getElementById('registerForm');
    registerForm.addEventListener('submit', function(event) {
        event.preventDefault();

        const email = document.getElementById('email').value;
        const password = document.getElementById('password').value;
        const confirmationPassword = document.getElementById('confirmation_password').value;

        // Check if the passwords match
        if (password !== confirmationPassword) {
            displayMessage('Passwords do not match.', 'error');
            return;
        }

        const formData = {
            user_email: email,
            user_password: password,
        };

        fetch('/register', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData),
        })
        .then(response => {
            if (!response.ok) {
                return response.json().then(data => {
                    throw new Error(data.message || 'Unknown error');
                });
            }
            return response.json();
        })
        .then(data => {
            displayMessage('Registration successful!', 'success');
            setTimeout(() => { window.location.href = '/login'; }, 3000);  // Redirect after successful registration
        })
        .catch(error => {
            displayMessage(error.message, 'error');  // Display the error message from the server
        });
    });

    function displayMessage(message, type) {
        const messageContainer = document.getElementById('messageContainer');
        messageContainer.textContent = message;
        messageContainer.className = 'message-container ' + type; // Apply error class for animation
    
        // Remove the animation class and re-add it for consecutive errors to restart the animation
        messageContainer.classList.remove('error'); // Reset class
        setTimeout(() => {
            messageContainer.classList.add(type); // Reapply class to trigger animation
        }, 1); // Short delay to re-trigger CSS animation
    }
});
