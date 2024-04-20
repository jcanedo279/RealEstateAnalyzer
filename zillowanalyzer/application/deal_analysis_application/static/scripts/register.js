import { flashMessage } from "./util/flashMessageHandlerUtil.js";

document.addEventListener('DOMContentLoaded', function() {
    const registerForm = document.getElementById('registerForm');
    const nextBtn = document.getElementById('nextBtn');
    const backBtn = document.getElementById('backBtn');
    showStep(1);

    nextBtn.addEventListener('click', () => showStep(2));
    backBtn.addEventListener('click', () => showStep(1));
    registerForm.addEventListener('submit', function(event) {
        event.preventDefault();

        const email = document.getElementById('email').value;
        const password = document.getElementById('password').value;
        const confirmationPassword = document.getElementById('confirmation_password').value;

        // Check if the passwords match
        if (password !== confirmationPassword) {
            flashMessage({message: 'Passwords do not match.', category: 'error', area: 'register', animation: 'shake'});
            return;
        }

        const formData = {
            firstName: document.getElementById('first_name').value,
            lastName: document.getElementById('last_name').value,
            userEmail: email,
            userPassword: password,
            isProfessional: document.getElementById('is_professional').value,
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
            setTimeout(() => {
                window.location.href = '/login';
            }, 2000);
        })
        .catch(error => {
            console.error('Error:', error);
        });
    });
});

function showStep(stepNumber) {
    // Hide all steps
    document.getElementById('formStep1').style.display = 'none';
    document.getElementById('formStep2').style.display = 'none';
    document.getElementById('nextBtn').style.display = 'none';
    document.getElementById('backBtn').style.display = 'none';
    document.getElementById('registerBtn').style.display = 'none';

    // Show the current step
    document.getElementById('formStep' + stepNumber).style.display = 'block';

    // Update active dot
    document.getElementById('dot1').classList.toggle('active', stepNumber === 1);
    document.getElementById('dot2').classList.toggle('active', stepNumber === 2);

    // Conditionally display buttons
    if (stepNumber === 1) {
        document.getElementById('nextBtn').style.display = 'inline-block';
    } else if (stepNumber === 2) {
        document.getElementById('backBtn').style.display = 'inline-block';
        document.getElementById('registerBtn').style.display = 'inline-block';
    }
}
