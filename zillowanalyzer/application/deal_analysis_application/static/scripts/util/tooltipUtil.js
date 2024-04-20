const tooltipUtility = (() => {
    const globalTooltip = document.createElement('div');

    function init() {
        globalTooltip.className = 'global-tooltip';
        globalTooltip.style.position = 'fixed';
        globalTooltip.style.display = 'none';
        globalTooltip.style.zIndex = '1000'; // Make sure it is on top
        document.body.appendChild(globalTooltip);
    }

    function showTooltip(e, key, description) {
        globalTooltip.innerHTML = '';

        const tooltipHeader = document.createElement('div');
        tooltipHeader.className = 'tooltip-header';
        tooltipHeader.textContent = key;

        globalTooltip.appendChild(tooltipHeader);

        if (description) {
            const tooltipBody = document.createElement('div');
            tooltipBody.className = 'tooltip-body';
            tooltipBody.textContent = description;
            globalTooltip.appendChild(tooltipBody);
        }
        
        // Make the tooltip invisible but renderable for measurement
        globalTooltip.style.visibility = 'hidden';
        globalTooltip.style.display = 'block';

        // Check and adjust tooltip position based on the cursor's position
        if (e.clientX > window.innerWidth/2) {
            globalTooltip.style.left = `${e.clientX - globalTooltip.offsetWidth - 10}px`; // Align left if it overflows the right viewport edge
        } else {
            globalTooltip.style.left = `${e.clientX + 15}px`; // Otherwise, position normally to the right
        }

        globalTooltip.style.top = `${e.clientY + 15}px`;
        globalTooltip.style.visibility = 'visible'; // Make the tooltip fully visible after positioning
        globalTooltip.style.width = "";
    }

    function hideTooltip() {
        globalTooltip.style.display = 'none';
    }

    function attachColumnTooltip(table, descriptions) {
        const headerRow = table.querySelector('thead tr');
        const bodyRows = table.querySelectorAll('tbody tr');

        headerRow.childNodes.forEach((th, columnIndex) => {
            const key = th.textContent;
            if (key !== "Property URL") {
                const description = descriptions[key] || false;
                bodyRows.forEach(row => {
                    const cell = row.childNodes[columnIndex];
                    if (cell) {
                        cell.addEventListener('mouseenter', (e) => showTooltip(e, key, description));
                        cell.addEventListener('mouseleave', hideTooltip);
                    }
                });
            }
        });
    }

    return { init, attachColumnTooltip };
})();

// Initialize the tooltip on document load
document.addEventListener('DOMContentLoaded', tooltipUtility.init);
