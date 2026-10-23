import type { DirectoryModelStatus } from './types';
import { formatElapsed, language } from './i18n';

export function elapsedLabel(seconds?: number) {
  if (seconds === undefined || !Number.isFinite(seconds)) return '';
  return formatElapsed(seconds);
}

export function modelStatusLabel(status?: DirectoryModelStatus) {
  if (status === 'waiting') return language.value === 'en' ? 'Waiting for the model' : '正在等待模型响应';
  if (status === 'connected') return language.value === 'en' ? 'Model connected, waiting for results' : '模型已连接，等待结果';
  if (status === 'thinking') return language.value === 'en' ? 'Model is analysing sources' : '模型正在分析资料';
  if (status === 'responding') return language.value === 'en' ? 'Receiving model results' : '正在接收模型结果';
  return '';
}
