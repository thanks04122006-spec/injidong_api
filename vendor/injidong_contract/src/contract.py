"""Injidong v1: replay/reference validation only; camera SDK and ML are adapters."""
from pathlib import Path
from dataclasses import dataclass
import hashlib
import json
import math
import re
import numpy as np

ROOT = Path(__file__).resolve().parents[1]

class ContractError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'{code}: {message}')

def fail(code, message):
    raise ContractError(code, message)

def _schema_check(value, schema, root, path='$'):
    """Validate only the keywords used by our bundled schemas (not a general library)."""
    if '$ref' in schema:
        target = root
        for part in schema['$ref'].removeprefix('#/').split('/'):
            target = target[part]
        return _schema_check(value, target, root, path)
    if 'anyOf' in schema:
        for candidate in schema['anyOf']:
            try:
                _schema_check(value, candidate, root, path)
                return
            except ContractError:
                pass
        fail('SCHEMA_INVALID', f'{path}: no permitted value matches')
    if 'enum' in schema and not any(type(value) is type(x) and value == x for x in schema['enum']):
        fail('SCHEMA_INVALID', f'{path}: unsupported value')
    checks = {'null':value is None, 'object':isinstance(value,dict),
              'array':isinstance(value,list), 'string':isinstance(value,str),
              'number':type(value) in (int,float) and math.isfinite(value),
              'integer':type(value) is int, 'boolean':type(value) is bool}
    if 'type' in schema and not checks[schema['type']]:
        fail('SCHEMA_INVALID', f'{path}: expected {schema["type"]}')
    if isinstance(value,dict):
        for key in schema.get('required',[]):
            if key not in value: fail('SCHEMA_INVALID', f'{path}.{key}: missing')
        properties = schema.get('properties',{})
        if schema.get('additionalProperties') is False and set(value)-set(properties):
            fail('SCHEMA_INVALID', f'{path}: unknown fields {set(value)-set(properties)}')
        for key in value.keys() & properties.keys():
            _schema_check(value[key],properties[key],root,path+'.'+key)
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0) or len(value)>schema.get('maxItems',math.inf):
            fail('SCHEMA_INVALID',f'{path}: array length')
        for i,item in enumerate(value):
            _schema_check(item,schema.get('items',{}),root,f'{path}[{i}]')
    if isinstance(value,str):
        if len(value)<schema.get('minLength',0): fail('SCHEMA_INVALID',f'{path}: empty string')
        if 'pattern' in schema and re.fullmatch(schema['pattern'],value) is None:
            fail('SCHEMA_INVALID',f'{path}: format')
    if type(value) in (int,float):
        if value<schema.get('minimum',-math.inf) or value>schema.get('maximum',math.inf):
            fail('SCHEMA_INVALID',f'{path}: range')
        if 'exclusiveMinimum' in schema and value<=schema['exclusiveMinimum']:
            fail('SCHEMA_INVALID',f'{path}: must be positive')

def validate_json(data, name):
    schema=json.loads((ROOT/'schemas'/f'{name}.schema.json').read_text(encoding='utf-8'))
    _schema_check(data,schema,schema)
    if name in ('processing','prediction'):
        success=data['status']=='success'
        if success and data['errors']: fail('RESULT_INVALID','success cannot have errors')
        if not success and not data['errors']: fail('RESULT_INVALID','failure/not_ready requires an error')
        if name=='prediction':
            if success and (data['predicted_weight_g'] is None or data['model_id'] is None):
                fail('RESULT_INVALID','success requires prediction and model_id')
            if not success and data['predicted_weight_g'] is not None:
                fail('RESULT_INVALID','failure/not_ready weight must be null')
            if data['quality']!='unknown' and (not success or data['quality_policy_id'] is None):
                fail('RESULT_INVALID','quality classification requires success and policy')
        if name=='processing':
            if not success and data['volume_m3'] is not None:
                fail('RESULT_INVALID','failed volume must be null')
            if success and data['stage']=='volume' and (data['volume_m3'] is None or data['volume_method'] is None):
                fail('RESULT_INVALID','volume success requires value and method')
            if success and data['stage']=='point_cloud' and data['point_cloud'] is None:
                fail('RESULT_INVALID','point cloud success requires file reference')
    if name=='features':
        if data['feature_version']=='volume_pose_v1' and data['pose_features'] is None:
            fail('POSE_REQUIRED','pose feature version requires tilt')
        if data['feature_version']=='volume_material_v1' and data['material'] is None:
            fail('MATERIAL_REQUIRED','material feature version requires material')

@dataclass
class FramePacket:
    metadata: dict
    depth_m: np.ndarray
    valid_mask: np.ndarray
    target_mask: np.ndarray | None = None

def normalize_depth(raw, scale_to_m, semantics='optical_axis_z', invalid_values=(0,)):
    """Adapter must obtain scale and semantics from actual SDK/profile, not dtype."""
    if type(scale_to_m) not in (int,float) or not math.isfinite(scale_to_m) or scale_to_m<=0:
        fail('UNIT_UNKNOWN','positive SDK/profile scale_to_m required')
    if semantics!='optical_axis_z': fail('SEMANTICS_UNSUPPORTED','convert ray distance with calibrated rays first')
    raw=np.asarray(raw)
    if raw.ndim!=2 or raw.size==0 or raw.dtype.kind not in 'uif':
        fail('DEPTH_SHAPE_INVALID','nonempty HxW numeric array required')
    with np.errstate(over='ignore',invalid='ignore'):
        depth=(raw.astype(np.float64)*scale_to_m).astype(np.float32)
    valid=np.isfinite(depth)&(depth>0)&np.isfinite(raw)
    for value in invalid_values: valid &= raw!=value
    depth[~valid]=0.0
    return depth,valid.astype(bool)

def _timestamp(t):
    empty=t['ns'] is None
    if empty != (t['clock_domain'] is None) or empty != (t['origin'] is None):
        fail('TIMESTAMP_INVALID','timestamp must be fully present or fully null')

def _pose(p):
    _timestamp(p['time'])
    if not np.isclose(np.linalg.norm(p['quaternion_xyzw']),1,atol=1e-5,rtol=0):
        fail('POSE_INVALID','quaternion_xyzw must have unit norm')
    if p['from_frame']==p['to_frame']: fail('POSE_INVALID','distinct coordinate frames required')

def validate_frame(frame, *, require_world_pose=False):
    m=frame.metadata
    validate_json(m,'frame')
    d,mask=frame.depth_m,frame.valid_mask
    shape=(m['depth']['height'],m['depth']['width'])
    if d.dtype!=np.float32 or mask.dtype!=np.bool_:
        fail('DEPTH_DTYPE_INVALID','depth float32 and mask bool required')
    if d.shape!=shape or mask.shape!=shape: fail('DEPTH_SHAPE_INVALID','depth/mask dimensions disagree')
    if not np.all(np.isfinite(d)) or np.any(d[mask]<=0) or np.any(d[~mask]!=0):
        fail('DEPTH_VALUES_INVALID','valid finite positive; invalid exactly zero')
    if not mask.any(): fail('NO_VALID_DEPTH','no usable depth samples')
    i=m['intrinsics']
    if (i['height'],i['width'])!=shape: fail('INTRINSICS_INVALID','must match depth profile')
    # Principal point may lie outside image in cropped/calibrated profiles; preserve SDK values.
    for key in ['capture_time','host_receive_time','replay_time']: _timestamp(m[key])
    if m['capture_time']['origin']=='replay_host': fail('TIMESTAMP_INVALID','replay cannot replace capture time')
    if m['host_receive_time']['ns'] is not None and m['host_receive_time']['origin']!='host_receive':
        fail('TIMESTAMP_INVALID','host receive origin required')
    if m['replay_time']['ns'] is not None and m['replay_time']['origin']!='replay_host':
        fail('TIMESTAMP_INVALID','replay origin required')
    if m['provenance']['kind']=='real' and (m['provenance']['camera'] is None or m['capture_source']=='synthetic'):
        fail('PROVENANCE_INVALID','real data requires camera and real capture source')
    if m['provenance']['kind']=='synthetic' and (m['provenance']['synthetic_seed'] is None or m['provenance']['synthetic_config'] is None or m['capture_source']!='synthetic'):
        fail('PROVENANCE_INVALID','synthetic seed/config/source required')
    if (m['provenance']['raw_file'] is None)!=(m['provenance']['raw_sha256'] is None):
        fail('PROVENANCE_INVALID','raw path and hash must be paired')
    if m['pose'] is not None:
        _pose(m['pose'])
        if m['pose']['from_frame']!='camera': fail('POSE_INVALID','frame pose must start at camera')
    if m['imu'] is not None:
        imu=m['imu']; _timestamp(imu['time'])
        if imu['orientation_xyzw'] is not None and not np.isclose(np.linalg.norm(imu['orientation_xyzw']),1,atol=1e-5,rtol=0):
            fail('POSE_INVALID','IMU orientation quaternion must have unit norm')
        if imu['camera_to_imu'] is not None:
            _pose(imu['camera_to_imu'])
            if imu['camera_to_imu']['from_frame']!='camera' or imu['camera_to_imu']['to_frame']!=imu['frame']:
                fail('POSE_INVALID','camera-to-IMU extrinsics mismatch')
    sync=m['sync']
    if sync['status']=='verified':
        if any(sync[key] is None for key in ['offset_ns','uncertainty_ns','tolerance_ns','evidence']):
            fail('SYNC_UNVERIFIED','measured sync fields and evidence required')
        if abs(int(sync['offset_ns']))+int(sync['uncertainty_ns'])>int(sync['tolerance_ns']):
            fail('SYNC_UNVERIFIED','residual time error exceeds recorded tolerance')
    if require_world_pose:
        if m['pose'] is None or m['pose']['to_frame']!='world': fail('POSE_REQUIRED','camera-to-world pose required')
        if m['capture_time']['ns'] is None or m['pose']['time']['ns'] is None: fail('TIMESTAMP_REQUIRED','capture and pose times required')
        if sync['status']!='verified': fail('SYNC_UNVERIFIED','world transform requires verified synchronization')
    if (frame.target_mask is None)!=(m['storage']['target_mask_key'] is None):
        fail('TARGET_INVALID','target key and array must both be present or absent')
    if (frame.target_mask is None)!=(m['target_mask_info'] is None):
        fail('TARGET_INVALID','target provenance required with mask')
    if frame.target_mask is not None and (frame.target_mask.dtype!=np.bool_ or frame.target_mask.shape!=shape):
        fail('TARGET_INVALID','target mask must be depth-aligned HxW bool')
    if m['support_plane'] is not None and not np.isclose(np.linalg.norm(m['support_plane']['normal']),1,atol=1e-5,rtol=0):
        fail('PLANE_INVALID','unit plane normal required')
    return frame

def _inside(base,path):
    candidate=(base/path).resolve()
    if not candidate.is_relative_to(base.resolve()): fail('PATH_INVALID','path escapes session directory')
    return candidate

def load_frame(metadata_path):
    path=Path(metadata_path)
    m=json.loads(path.read_text(encoding='utf-8'))
    validate_json(m,'frame')
    arrays_path=_inside(path.parent,m['storage']['file'])
    if hashlib.sha256(arrays_path.read_bytes()).hexdigest()!=m['storage']['sha256']:
        fail('INPUT_CORRUPT','arrays.npz checksum mismatch')
    try:
        with np.load(arrays_path,allow_pickle=False) as arrays:
            expected={'depth_m','valid_mask'}|({'target_mask'} if m['storage']['target_mask_key'] else set())
            if set(arrays.files)!=expected: fail('INPUT_CORRUPT','unexpected or missing NPZ keys')
            frame=FramePacket(m,arrays['depth_m'],arrays['valid_mask'],arrays['target_mask'] if 'target_mask' in arrays.files else None)
    except (ValueError,KeyError,OSError) as e:
        if isinstance(e,ContractError): raise
        fail('INPUT_CORRUPT',str(e))
    return validate_frame(frame)

def iter_frames(session_dir):
    base=Path(session_dir)
    seen=set()
    for line in (base/'frames.jsonl').read_text(encoding='utf-8').splitlines():
        if not line.strip(): continue
        entry=json.loads(line)
        if set(entry)!={'frame_id','metadata_path'}: fail('MANIFEST_INVALID','two manifest fields required')
        if entry['frame_id'] in seen: fail('MANIFEST_INVALID','duplicate frame_id')
        seen.add(entry['frame_id'])
        f=load_frame(_inside(base,entry['metadata_path']))
        if f.metadata['frame_id']!=entry['frame_id']: fail('MANIFEST_INVALID','frame_id mismatch')
        yield f

def point_cloud_camera(frame):
    validate_frame(frame)
    v,u=np.nonzero(frame.valid_mask)
    z=frame.depth_m[v,u]; i=frame.metadata['intrinsics']
    return np.column_stack(((u-i['cx'])*z/i['fx'],(v-i['cy'])*z/i['fy'],z)).astype(np.float32)

def volume_preflight(frame):
    """Validate prerequisites. Passing does not mean volume has been computed."""
    validate_frame(frame)
    if frame.target_mask is None or not np.any(frame.target_mask): fail('TARGET_UNAVAILABLE','nonempty target mask required')
    if frame.metadata['support_plane'] is None: fail('SUPPORT_PLANE_REQUIRED','support plane required')
    r=frame.metadata['volume_request']
    if r is None or not r['assumptions_confirmed']: fail('VOLUME_ASSUMPTION_REQUIRED','known footprint and fully observed top required')
    b=r['plane_basis']; u=np.array(b['u_axis']); v=np.array(b['v_axis'])
    p=frame.metadata['support_plane']; n=np.array(p['normal']); origin=np.array(b['origin_m'])
    if not (np.isclose(np.linalg.norm(u),1,atol=1e-5,rtol=0) and np.isclose(np.linalg.norm(v),1,atol=1e-5,rtol=0) and np.isclose(u@v,0,atol=1e-5) and np.allclose(np.cross(u,v),n,atol=1e-5,rtol=0) and np.isclose(n@origin+p['offset_m'],0,atol=1e-5)):
        fail('PLANE_BASIS_INVALID','right-handed orthonormal basis on support plane required')
    if np.any(frame.target_mask & ~frame.valid_mask): fail('COVERAGE_INSUFFICIENT','target contains missing depth; no silent zero-fill')
    return True

def model_not_ready(features):
    validate_json(features,'features')
    result={'schema_version':'1.0.0','frame_id':features['frame_id'],'status':'not_ready','predicted_weight_g':None,'model_id':None,'feature_version':features['feature_version'],'provenance_kind':features['provenance_kind'],'quality':'unknown','quality_policy_id':None,'errors':[{'code':'MODEL_NOT_READY','message':'No trained model has been registered.'}],'warnings':[]}
    validate_json(result,'prediction')
    return result

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('session',nargs='?',default=str(ROOT/'examples/synthetic'))
    args=parser.parse_args()
    for frame in iter_frames(args.session):
        points=point_cloud_camera(frame)
        print(json.dumps({'frame_id':frame.metadata['frame_id'],'provenance_kind':frame.metadata['provenance']['kind'],'point_count':len(points),'points_m':points.tolist()},allow_nan=False))
