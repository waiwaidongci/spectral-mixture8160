import {request} from './client';
import {ImpactReport,SpectrumSource} from '../types/domain';

export const getSources=(sampleId:number)=>request<SpectrumSource[]>(`/samples/${sampleId}/sources`);
export const revokeSource=(sampleId:number,sourceId:number,reason:string)=>request<SpectrumSource>(`/samples/${sampleId}/sources/${sourceId}/revoke`,{method:'POST',body:JSON.stringify({reason})});
export const registerReplacement=(sampleId:number,observed:number[],reason:string)=>request<SpectrumSource>(`/samples/${sampleId}/sources/replacement`,{method:'POST',body:JSON.stringify({observed,reason})});
export const getImpact=(libraryId?:number)=>request<ImpactReport>('/source-impact'+(libraryId?`?library_id=${libraryId}`:''));
export const retryReplacement=(runId:number)=>request<{created:number[];requeued:number[]}>(`/runs/${runId}/replacement/retry`,{method:'POST'});
