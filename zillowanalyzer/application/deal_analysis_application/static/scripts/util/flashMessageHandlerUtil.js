export function handleMessages(messages) {
    messages.forEach(message => {
        handleMessage(message)
    });
}

export function flashMessage({message, category = 'info', area = 'default', animation = ''}) {
    handleMessage({
        'message': message,
        'category': category,
        'area': area,
        'animation': animation
    });
}

function handleMessage({message, category, area, animation}) {
    const container = document.querySelector(`[data-flash-area="${area}"]`);
        if (container) {
            displayMessage(container, {message, category, animation});
        }
}

function displayMessage(container, {message, category, animation}) {
    container.innerHTML = ''; // Optionally clear previous messages
    const div = document.createElement('div');
    div.className = `alert ${category} ${animation || ''}`;
    div.textContent = message;
    container.appendChild(div);
}
