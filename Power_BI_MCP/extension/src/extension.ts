import * as vscode from 'vscode';
import { Logger } from './logger';
import { MCPServerManager } from './mcpServerManager';

const FIRST_ACTIVATION_KEY = 'powerbi-modeling-mcp.firstActivation';

export async function activate(context: vscode.ExtensionContext) {
  // Create logger instance
  const logger = new Logger('Power BI Modeling MCP');
  logger.info('Extension activating...');

  // Register MCP server definition provider
  const provider = new MCPServerManager(logger, context.extensionPath);
  const disposable = vscode.lm.registerMcpServerDefinitionProvider(
    'powerbi-modeling-mcp.servers',
    provider
  );
  
  context.subscriptions.push(disposable);
  logger.info('MCP server definition provider registered');

  // Check if this is the first activation
  const isFirstActivation = !context.globalState.get(FIRST_ACTIVATION_KEY, false);

  if (isFirstActivation) {
    // Show success notification only on first activation
    vscode.window.showInformationMessage(
      'Power BI Modeling MCP Server registered successfully!'
    );

    // Update global state
    await context.globalState.update(FIRST_ACTIVATION_KEY, true);
    logger.info('First activation notification shown');
  }

  // Register disposables
  context.subscriptions.push(logger);

  logger.info('Extension activated');
}

export async function deactivate() {
  // Cleanup is handled by disposables registered in activate
}
