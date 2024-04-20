export class ListingsRetriever {
    /**
        * @param {Function} formDataRetriever A function which retrieves the query data from the form.
        * @param {String} route A string representing the route to send the formData to, starting with a '/' literal.
    */
    constructor(formDataRetriever, route) {
        this.page = 1;
        this.total_pages = 0;
        this.formDataRetriever = formDataRetriever;
        this.route = route;
    }

    updatePageIndicator() {
        const pageIndicator = document.getElementById('pageIndicator');
        pageIndicator.textContent = `Page ${this.page}`;

        // Disable previous button if on the first page
        const prevPageBtn = document.getElementById('prevPageBtn');
        if (this.page <= 1) {
            prevPageBtn.classList.add('disabled');
            prevPageBtn.setAttribute('disabled', 'disabled');
        } else {
            prevPageBtn.classList.remove('disabled');
            prevPageBtn.removeAttribute('disabled');
        }
    
        // Disable next button if on the last page
        const nextPageBtn = document.getElementById('nextPageBtn');
        if (this.page >= this.total_pages) {
            nextPageBtn.classList.add('disabled');
            nextPageBtn.setAttribute('disabled', 'disabled');
        } else {
            nextPageBtn.classList.remove('disabled');
            nextPageBtn.removeAttribute('disabled');
        }
    }
    
    setPage(newPage) {
        this.page = newPage;
        this.updatePageIndicator();
        this.fetchListings();
    }

    getFormData() {
        // Combines the form data with the current page.
        return {
            ...this.formDataRetriever(),
            current_page: this.page
        };
    }

    fetchListings() {
        fetch(this.route, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this.getFormData()),
        })
        .then(response => response.json())
        .then(data => {
            const resultsDiv = document.getElementById('resultsTable');
            // Clear previous results.
            resultsDiv.innerHTML = '';
            // Get the total pages fromt he server.
            this.total_pages = data.total_pages;
    
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
                table.className = 'highlight table-bordered table-striped';
    
                // Create header row
                const thead = document.createElement('thead');
                const headerRow = document.createElement('tr');
                Object.keys(data.properties[0]).forEach(key => {
                    if (key !== "property_url" && key !== "zpid") {
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
                        if (key !== 'property_url' && key !== 'zpid') {
                            const td = document.createElement('td');
                            if (key === 'Image' && value) {
                                const imgLink = document.createElement('a');
                                imgLink.href = item['property_url']; // Use the property URL
                                imgLink.target = "_blank"; // Open in a new tab
        
                                const img = document.createElement('img');
                                img.src = value;
                                img.style.maxHeight = '150px';
                                img.style.maxWidth = '150px';
        
                                imgLink.appendChild(img);
                                td.appendChild(imgLink);
                            } else if (key === 'Save') {
                                const saveBtn = document.createElement('button');
                                console.log(value)
                                saveBtn.innerHTML = value ? '<i class="fas fa-star"></i>' : '<i class="far fa-star"></i>';
                                saveBtn.classList.add('save-btn');
                                saveBtn.onclick = () => this.toggleSave(item.zpid, saveBtn);
                                td.appendChild(saveBtn);
                            } else {
                                td.textContent = value;
                            }
                            row.appendChild(td);
                        }
                    });
                    tbody.appendChild(row);
                });
                table.appendChild(tbody);
    
                // Append table to the div
                resultsDiv.appendChild(table);
                // Append a column-based tooltip to the table.
                tooltipUtility.attachColumnTooltip(document.querySelector('.highlight'), data.descriptions);
    
                this.updatePageIndicator();
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
            if (this.total_pages > 1) {
                // Show navigation buttons.
                document.getElementById('navigationButtons').style.visibility = 'visible';
            } else {
                // Optionally hide navigation buttons if no properties found.
                document.getElementById('navigationButtons').style.visibility = 'hidden';
            }
        })
        .catch(error => console.error('Error:', error));
    }

    initPageEvents() {
        document.getElementById("prevPageBtn").onclick = () => {
            if(this.page > 1) this.setPage(this.page - 1);
        };

        document.getElementById("nextPageBtn").onclick = () => {
            if(this.page < this.total_pages) this.setPage(this.page + 1);
        };

        document.getElementById('baseBody').addEventListener('keypress', event => {
            if (event.key === 'Enter' || event.keyCode === 13) {
                event.preventDefault();
                this.setPage(1);
            }
        });

        const submitButton = document.getElementById("submitBtn");
        if (submitButton) {
            submitButton.onclick = () => this.setPage(1);
        }
    }

    toggleSave(propertyId, button) {
        fetch('/toggle-save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ propertyId }),
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Update the button icon based on the saved state
                button.innerHTML = data.saved ? '<i class="fas fa-star"></i>' : '<i class="far fa-star"></i>';
                button.classList.toggle('saved', data.saved); // Add/remove saved class for styling
            } else {
                alert(data.error || 'Failed to toggle save state.');
            }
        })
        .catch(error => console.error('Error:', error));
    }
}
