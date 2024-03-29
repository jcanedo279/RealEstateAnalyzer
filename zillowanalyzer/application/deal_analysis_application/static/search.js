document.getElementById("submitBtn").onclick = function() {
    const formData = {
        property_id: document.getElementById("property_id").value,
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

        if (data.properties && data.properties.length > 0) {
            // Create a table
            const table = document.createElement('table');
            table.className = 'striped responsive-table table-bordered';

            // Create header row
            const thead = document.createElement('thead');
            const headerRow = document.createElement('tr');
            Object.keys(data.properties[0]).forEach(key => {
                if (key !== "property_url") {
                    const th = document.createElement('th');
                    th.textContent = key;
                    headerRow.appendChild(th);
                }
            });
            thead.appendChild(headerRow);
            table.appendChild(thead);

            // Populate table body
            const tbody = document.createElement('tbody');
            data.properties.forEach(item => {
                const row = document.createElement('tr');
                Object.entries(item).forEach(([key, value]) => {
                    const td = document.createElement('td');
                    if (key === 'Image' && value) {
                        const imgLink = document.createElement('a');
                        imgLink.href = item['property_url']; // Use the property URL
                        imgLink.target = "_blank"; // Open in a new tab

                        const img = document.createElement('img');
                        img.src = value;
                        img.style.maxWidth = '150px'; // Set image size

                        imgLink.appendChild(img);
                        td.appendChild(imgLink);
                    } else if (key !== 'property_url') {
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
            const notFoundDiv = document.createElement('div');
            notFoundDiv.textContent = `No Properties Found :<`;
            notFoundDiv.style.fontSize = '24px';
            notFoundDiv.style.fontWeight = 'bold';
            notFoundDiv.style.marginBottom = '20px';
            notFoundDiv.style.marginTop = '20px';
            resultsDiv.appendChild(notFoundDiv);
        }
    })
    .catch((error) => {
        console.error('Error:', error);
    });
};
