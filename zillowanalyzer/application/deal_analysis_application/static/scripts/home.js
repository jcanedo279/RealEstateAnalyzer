document.getElementById("issueReportForm").addEventListener('submit', function(event) {
    document.getElementById('issueReportForm').reset();
    event.preventDefault();
    reportIssue();
});

function reportIssue() {
    const formData = {
        user_email: document.getElementById("user_email").value,
        issue_description: document.getElementById("issue_description").value,
    };

    // Disable the button to prevent multiple submissions.
    document.getElementById("reportIssueBtn").disabled = true;

    fetch('/report', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(formData),
    })
    .then(response => {
        // Re-enable form submission.
        document.getElementById("reportIssueBtn").disabled = false;
        if (!response.ok) {
            throw new Error('Network response was not ok ' + response.statusText);
        }
        return response.json();
    })
    .then(data => {
        console.log('Success:', data);
        showModal();
        // Reset the form.
        document.getElementById("issueReportForm").reset();
    })
    .catch((error) => {
        console.error('Error:', error);
        alert("There was a problem submitting your report. Please try again.");
    });
}

function showModal() {
    document.getElementById('confirmationModal').style.display = 'block';
}

document.querySelector('.modal-close').onclick = function() {
    document.getElementById('confirmationModal').style.display = 'none';
}
