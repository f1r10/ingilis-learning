import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "./api/client";
import "./i18n";
import "./index.css";
import App from "./App";
import { AppProvider } from "./context/AppProvider";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      // A refusal the server answered is deterministic: retrying "this paper is not yours" would
      // only delay the sentence, and react-query pauses a retry while the tab is unfocused or its
      // `navigator.onLine` says offline - which left the screen spinning forever over a 404 it had
      // already received. Only a request that never got an answer is worth a second attempt.
      retry: (failureCount, error) => failureCount < 1 && (error as ApiError)?.status === 0,
      // `navigator.onLine` lies on some browsers and webviews, so the first attempt goes out
      // regardless rather than waiting for a signal that may never arrive.
      networkMode: "offlineFirst",
    },
    mutations: { networkMode: "offlineFirst" },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <AppProvider>
        <App />
      </AppProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
