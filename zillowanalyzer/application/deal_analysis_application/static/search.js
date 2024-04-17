// Initialize currentPage globally
let page = 1;
let total_pages = 0


function updatePageIndicator() {
    const pageIndicator = document.getElementById('pageIndicator');
    pageIndicator.textContent = `Page ${page}`;

    // Disable previous button if on the first page
    const prevPageBtn = document.getElementById('prevPageBtn');
    if (page <= 1) {
        prevPageBtn.classList.add('disabled');
        prevPageBtn.setAttribute('disabled', 'disabled');
    } else {
        prevPageBtn.classList.remove('disabled');
        prevPageBtn.removeAttribute('disabled');
    }

    // Disable next button if on the last page
    const nextPageBtn = document.getElementById('nextPageBtn');
    if (page >= total_pages) {
        nextPageBtn.classList.add('disabled');
        nextPageBtn.setAttribute('disabled', 'disabled');
    } else {
        nextPageBtn.classList.remove('disabled');
        nextPageBtn.removeAttribute('disabled');
    }
}

// Update the page indicator and button states immediately after setting the new page value
function setPage(newPage) {
    page = newPage;
    updatePageIndicator();
    fetchSearchData();
}

document.addEventListener('DOMContentLoaded', function() {
    var elems = document.querySelectorAll('select');
    M.FormSelect.init(elems, {});
});

document.getElementById("prevPageBtn").onclick = function() {
    if(page > 1) {
        setPage(page - 1);
    }
};
document.getElementById("nextPageBtn").onclick = function() {
    if(page < total_pages) {
        setPage(page + 1);
    }
};

document.getElementById('baseBody').addEventListener('keypress', function(event) {
    // Check if the Enter key was pressed
    if (event.key === 'Enter' || event.keyCode === 13) {
        event.preventDefault();
        fetchSearchData()
    }
});

document.getElementById("submitBtn").onclick = function() {
    fetchSearchData()
};

function fetchSearchData() {
    const formData = {
        property_address: document.getElementById("property_address").value,
        current_page: page,
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
        // Clear previous results.
        resultsDiv.innerHTML = '';
        // Get the total pages fromt he server.
        total_pages = data.total_pages;

        if (data.properties && data.properties.length > 0) {
            const existingCount = document.getElementById('totalCountDiv');
            if (existingCount) {
                existingCount.remove(); // Remove the existing count before adding a new one to avoid duplicates
            }

            const totalCountDiv = document.createElement('div');
            totalCountDiv.id = 'totalCountDiv'; // Ensure it can be uniquely identified
            totalCountDiv.className = 'total-count'; // This class will be styled in CSS

            // Adding more structure to the content
            totalCountDiv.innerHTML = `
                <i class="fas fa-home"></i>
                <span>Total Properties: ${data.total_properties}</span>
            `;
            resultsDiv.appendChild(totalCountDiv);

            // Create a table
            const table = document.createElement('table');
            table.className = 'striped responsive-table table-bordered table-striped';

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
            // Append a column-based tooltip to the table.
            tooltipUtility.attachColumnTooltip(document.querySelector('.striped'), data.descriptions);

            updatePageIndicator();
        } else {
            const notFoundDiv = document.createElement('div');
            notFoundDiv.textContent = `No Properties Found :<`;
            notFoundDiv.style.fontSize = '24px';
            notFoundDiv.style.fontWeight = 'bold';
            notFoundDiv.style.marginBottom = '20px';
            notFoundDiv.style.marginTop = '20px';
            resultsDiv.appendChild(notFoundDiv);
        }

        // Conditionally display the navigation buttons.
        if (total_pages > 1) {
            // Show navigation buttons.
            document.getElementById('navigationButtons').style.visibility = 'visible';
        } else {
            // Optionally hide navigation buttons if no properties found.
            document.getElementById('navigationButtons').style.visibility = 'hidden';
        }
    })
    .catch((error) => {
        console.error('Error:', error);
    });
}
