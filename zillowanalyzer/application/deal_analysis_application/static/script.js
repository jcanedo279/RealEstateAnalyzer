document.getElementById("submitBtn").onclick = function() {
    const formData = {
        region: document.getElementById("region").value,
        home_type: document.getElementById("home_type").value,
        year_built: document.getElementById("year_built").value,
        max_price: document.getElementById("max_price").value,
        is_waterfront: document.getElementById("is_waterfront").checked,
        num_deals: document.getElementById("num_deals").value,
    };

    fetch('/search', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(formData),
    })
    .then(response => response.json())
    .then(data => {
        const resultsDiv = document.getElementById('resultsTable');
        resultsDiv.innerHTML = ''; // Clear previous results

        // Display total properties count or a no properties found message
        if (data.properties && data.properties.length > 0) {
            const totalCountDiv = document.createElement('div');
            totalCountDiv.textContent = `Total Properties: ${data.total_properties}`;
            resultsDiv.appendChild(totalCountDiv);

            // Create a table
            const table = document.createElement('table');
            table.className = 'striped responsive-table';

            // Create header row
            const thead = document.createElement('thead');
            const headerRow = document.createElement('tr');
            Object.keys(data.properties[0]).forEach(key => {
                const th = document.createElement('th');
                th.textContent = key;
                headerRow.appendChild(th);
            });
            thead.appendChild(headerRow);
            table.appendChild(thead);

            // Populate table body
            const tbody = document.createElement('tbody');
            data.properties.forEach(item => {
                const row = document.createElement('tr');
                Object.entries(item).forEach(([key, value]) => {
                    const td = document.createElement('td');
                    if (key === 'image_url' && value) {
                        const img = document.createElement('img');
                        img.src = value;
                        img.style.maxWidth = '150px'; // Set image size
                        td.appendChild(img);
                    } else {
                        td.textContent = value;
                    }
                    row.appendChild(td);
                });
                tbody.appendChild(row);
            });
            table.appendChild(tbody);

            // Append table to the div
            resultsDiv.appendChild(table);
        } else {
            // Display a message indicating no properties were found
            const noPropertiesDiv = document.createElement('div');
            noPropertiesDiv.textContent = 'No properties found that match the criteria.';
            resultsDiv.appendChild(noPropertiesDiv);
        }
    })
    .catch((error) => {
        console.error('Error:', error);
    });
};

document.addEventListener('DOMContentLoaded', function() {
    var elems = document.querySelectorAll('select');
    var instances = M.FormSelect.init(elems, {});
});
