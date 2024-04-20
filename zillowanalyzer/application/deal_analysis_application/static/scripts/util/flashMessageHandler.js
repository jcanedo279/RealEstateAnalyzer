import { handleMessages } from "./flashMessageHandlerUtil.js";

class FlashMessageHandler {
    constructor() {
        this.init();
    }

    init() {
        // Automatically attach the handler when the document is fully loaded
        document.addEventListener('DOMContentLoaded', () => {
            this.setupGlobalFetchInterceptor();
        });
    }

    setupGlobalFetchInterceptor() {
        // Save a reference to the original fetch function
        const originalFetch = window.fetch;

        // Define a new fetch that incorporates our custom logic
        window.fetch = async (...args) => {
            const response = await originalFetch(...args);
            const data = await response.clone().json();
            if (data.fancy_flash_messages) {
                handleMessages(data.fancy_flash_messages);
            }
            return response;
        };
    }
}


new FlashMessageHandler();
