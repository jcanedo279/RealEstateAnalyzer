document.addEventListener('DOMContentLoaded', function() {
    var loginForm = document.getElementById('loginForm');
    
    loginForm.addEventListener('submit', function(event) {
        event.preventDefault();

        const formData = {
            user_email: document.getElementById('email').value,
            user_password: document.getElementById('password').value,
        };

        // Disable the button to prevent multiple submissions.
        document.getElementById("loginBtn").disabled = true;

        // Simple client-side validation example
        if(email === "" || password === "") {
            alert("Please enter both email and password.");
            return;
        }

        fetch('/login', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData),
        })
        .then(response => {
            // Re-enable form submission.
            document.getElementById("loginBtn").disabled = false;
            if (response.redirected) {
                window.location.href = response.url;
                return; // Important to stop processing further as we are redirecting
            }
            return response.json();
        })
        .then(data => {
            console.log('Success:', data);
            // Reset the form.
            document.getElementById("loginForm").reset();
        })
        .catch((error) => {
            console.error('Error:', error);
            alert("There was a problem submitting your report. Please try again.");
        });
    });
});
