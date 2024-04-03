let mousePaths = [];
let recording = false;

document.getElementById('captcha-button').addEventListener('mousedown', function(event) {
    recording = true;
    mousePaths = [['start', event.pageX, event.pageY]]; // Initialize recording
});

document.addEventListener('mousemove', function(event) {
    if (recording) {
        mousePaths.push([event.pageX, event.pageY]);
    }
});

document.getElementById('captcha-button').addEventListener('mouseup', function() {
    recording = false;
    mousePaths.push(['end', mousePaths[mousePaths.length - 1][1], mousePaths[mousePaths.length - 1][2]]);
    
    // Send mousePaths to the server
    fetch('/save-path', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(mousePaths),
    })
    .then(response => response.json())
    .then(data => {
        console.log('Success:', data);
    })
    .catch((error) => {
        console.error('Error:', error);
    });

    console.log('Mouse path:', mousePaths);
});
