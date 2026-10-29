/**
 * Top-level error boundary (PLAN.md §11.2).
 *
 * A React render error with no boundary unmounts the whole tree and leaves a blank
 * window — indistinguishable from "the app didn't open". This renders the error
 * instead, so a failure is visible and reportable.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

import { Button } from "./ui";

type Props = { children: ReactNode };
type State = { error: Error | null; info: string | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ info: info.componentStack ?? null });
    console.error("StackSearch UI error:", error, info);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="fatal">
        <div className="fatal-card" role="alert">
          <b>Something went wrong</b>
          <p>
            The interface hit an error. Take a screenshot of this message and send it
            back.
          </p>
          <pre>
            {this.state.error.message}
            {this.state.info ? `\n${this.state.info}` : ""}
          </pre>
          <div className="dialog-actions">
            <Button onClick={() => window.location.reload()}>Reload window</Button>
          </div>
        </div>
      </div>
    );
  }
}
